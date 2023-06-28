import base64
import datetime
import io
import json
import math
import os
import re
import traceback
from ast import literal_eval
from typing import Mapping

import aiofiles
import aiohttp
import aiohttp_jinja2
import jinja2
import jwt
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pymongo
from aiohttp import multipart, web
from aiohttp.web_request import MultiDictProxy
from aiohttp_session import get_session, setup
from aiohttp_session.cookie_storage import EncryptedCookieStorage
from bson.json_util import dumps, loads
from misaka import HtmlRenderer, Markdown
from motor.motor_asyncio import AsyncIOMotorClient
from penquins import Kowalski
from utils import (
    alphabet2num,
    check_password_hash,
    compute_hash,
    generate_password_hash,
    get_rgb_ps_stamp_url,
    great_circle_distance,
    lc_colors,
    mjd_to_datetime,
    num2alphabet,
    parse_radec,
    radec2lb,
    radec_str2geojson,
    radec_str2rad,
    random_alphanumeric_str,
    to_pretty_json,
    uid,
    utc_now,
)

""" markdown rendering """
rndr = HtmlRenderer()
md = Markdown(rndr, extensions=("fenced-code",))

current_dir = os.path.dirname(os.path.abspath(__file__))


""" load config and secrets """
with open(current_dir + "/config.json", "r") as cjson:
    config = json.load(cjson)

with open(current_dir + "/secrets.json", "r") as sjson:
    secrets = json.load(sjson)

for k in secrets:
    if k in config:
        config[k].update(secrets.get(k, {}))
    else:
        config[k] = secrets[k]


def init_paths():
    paths = config["path"]
    for k in paths:
        if not os.path.exists(paths[k]):
            try:
                os.makedirs(paths[k])
            except Exception as e:
                print(f"Got error: {str(e)}")
                _err = traceback.format_exc()
                print(_err)
                continue


init_paths()


async def init_db():
    if config["database"].get("srv", False) is True:
        conn_string = "mongodb+srv://"
    else:
        conn_string = "mongodb://"

    if (
        config["database"]["admin"] is not None
        and config["database"]["admin_pwd"] is not None
    ):
        conn_string += (
            f"{config['database']['admin']}:{config['database']['admin_pwd']}@"
        )

    conn_string += f"{config['database']['host']}"
    if config["database"].get("srv", False) is not True:
        conn_string += f":{config['database']['port']}"

    if config["database"].get("replica_set", None) is not None:
        conn_string += f"/?replicaSet={config['database']['replica_set']}"

    _client = AsyncIOMotorClient(conn_string)

    # _id: db_name.user_name
    user_ids = []
    async for _u in _client.admin.system.users.find({}, {"_id": 1}):
        user_ids.append(_u["_id"])

    print(user_ids)

    db_name = config["database"]["db"]
    username = config["database"]["user"]

    # print(f'{db_name}.{username}')
    # print(user_ids)

    _mongo = _client[db_name]

    if f"{db_name}.{username}" not in user_ids:
        await _mongo.command(
            "createUser",
            config["database"]["user"],
            pwd=config["database"]["pwd"],
            roles=["readWrite"],
        )
        print("Successfully initialized db")

    _mongo.client.close()


async def add_admin(_mongo):
    """
        Create admin user for the web interface if it does not exist already
    :return:
    """
    ex_admin = await _mongo.users.find_one({"_id": config["server"]["admin_username"]})
    if ex_admin is None or len(ex_admin) == 0:
        try:
            await _mongo.users.insert_one(
                {
                    "_id": config["server"]["admin_username"],
                    "password": generate_password_hash(
                        config["server"]["admin_password"]
                    ),
                    "permissions": {},
                    "last_modified": utc_now(),
                }
            )
        except Exception as e:
            print(f"Got error: {str(e)}")
            _err = traceback.format_exc()
            print(_err)


async def add_master_program(_mongo):
    """
        Create program id 1 if it does not exist already
    :param _mongo:
    :return:
    """
    # get number of programs
    ex_program_1 = await _mongo.programs.find_one({"_id": 1})

    # add program to programs collection:
    if ex_program_1 is None or len(ex_program_1) == 0:
        try:
            await _mongo.programs.insert_one(
                {
                    "_id": 1,
                    "name": "skipper",
                    "description": "default program",
                    "last_modified": utc_now(),
                }
            )
        except Exception as e:
            print(f"Got error: {str(e)}")
            _err = traceback.format_exc()
            print(_err)


routes = web.RouteTableDef()


@web.middleware
async def auth_middleware(request, handler):
    """
        auth middleware
    :param request:
    :param handler:
    :return:
    """
    # tic = time.time()
    request.user = None
    jwt_token = request.headers.get("authorization", None)

    if jwt_token is not None:
        try:
            payload = jwt.decode(
                jwt_token,
                request.app["JWT"]["JWT_SECRET"],
                algorithms=[request.app["JWT"]["JWT_ALGORITHM"]],
            )
            # print('Godny token!')
        except (jwt.DecodeError, jwt.ExpiredSignatureError):
            return web.json_response({"message": "Token is invalid"}, status=400)

        request.user = payload["user_id"]

    response = await handler(request)
    # toc = time.time()
    # print(f"Auth middleware took {toc-tic} seconds to execute")

    return response


def auth_required(func):
    """
        Wrapper to ensure successful user authorization to use the API
    :param func:
    :return:
    """

    def wrapper(request):
        if not request.user:
            return web.json_response({"message": "Auth required"}, status=401)
        return func(request)

    return wrapper


def login_required(func):
    """
        Wrapper to ensure successful user authorization to use both the API and the web frontend
    :param func:
    :return:
    """

    async def wrapper(request):
        # if request.user:
        #     return await func(request)
        # get session:
        session = await get_session(request)
        # print(session)
        if "jwt_token" not in session:
            # return web.json_response({'message': 'Auth required'}, status=401)
            # redirect to login page
            location = request.app.router["login"].url_for()
            # location = '/login'
            raise web.HTTPFound(location=location)
        else:
            jwt_token = session["jwt_token"]
            if not await token_ok(request, jwt_token):
                # return web.json_response({'message': 'Auth required'}, status=401)
                # redirect to login page
                location = request.app.router["login"].url_for()
                # location = '/login'
                raise web.HTTPFound(location=location)
        return await func(request)

    return wrapper


async def token_ok(request, jwt_token):
    try:
        jwt.decode(
            jwt_token,
            request.app["JWT"]["JWT_SECRET"],
            algorithms=[request.app["JWT"]["JWT_ALGORITHM"]],
        )
        return True
    except (jwt.DecodeError, jwt.ExpiredSignatureError):
        return False


@routes.post("/auth")
async def auth(request):
    try:
        post_data = await request.json()
    except Exception as _e:
        print(f"Cannot extract json() from request, trying post(): {str(_e)}")
        # _err = traceback.format_exc()
        # print(_err)
        post_data = await request.post()

    # print(post_data)

    # must contain 'username' and 'password'

    if ("username" not in post_data) or (len(post_data["username"]) == 0):
        return web.json_response({"message": 'Missing "username"'}, status=400)
    if ("password" not in post_data) or (len(post_data["password"]) == 0):
        return web.json_response({"message": 'Missing "password"'}, status=400)

    username = str(post_data["username"])
    password = str(post_data["password"])

    try:
        # user exists and passwords match?
        select = await request.app["mongo"].users.find_one({"_id": username})
        if check_password_hash(select["password"], password):
            payload = {
                "user_id": username,
                "exp": datetime.datetime.utcnow()
                + datetime.timedelta(
                    seconds=request.app["JWT"]["JWT_EXP_DELTA_SECONDS"]
                ),
            }
            jwt_token = jwt.encode(
                payload,
                request.app["JWT"]["JWT_SECRET"],
                request.app["JWT"]["JWT_ALGORITHM"],
            )

            return web.json_response({"token": jwt_token})

        else:
            return web.json_response({"message": "Wrong credentials"}, status=400)

    except Exception as e:
        print(f"Got error: {str(e)}")
        _err = traceback.format_exc()
        print(_err)
        return web.json_response({"message": "Wrong credentials"}, status=400)


@routes.get("/login")
async def login_get(request):
    """
        Serve login page
    :param request:
    :return:
    """
    context = {"logo": config["server"]["logo"]}
    response = aiohttp_jinja2.render_template("template-login.html", request, context)
    return response


@routes.post("/login", name="login")
async def login_post(request):
    """
        Server login page for the browser
    :param request:
    :return:
    """
    try:
        try:
            post_data = await request.json()
        except Exception as _e:
            print(f"Cannot extract json() from request, trying post(): {str(_e)}")
            # _err = traceback.format_exc()
            # print(_err)
            post_data = await request.post()

        # get session:
        session = await get_session(request)

        if ("username" not in post_data) or (len(post_data["username"]) == 0):
            return web.json_response({"message": 'Missing "username"'}, status=400)
        if ("password" not in post_data) or (len(post_data["password"]) == 0):
            return web.json_response({"message": 'Missing "password"'}, status=400)

        username = str(post_data["username"])
        password = str(post_data["password"])

        # print(username, password)
        print(f"User {username} logged in.")

        # user exists and passwords match?
        select = await request.app["mongo"].users.find_one({"_id": username})
        if check_password_hash(select["password"], password):
            payload = {
                "user_id": username,
                "exp": datetime.datetime.utcnow()
                + datetime.timedelta(
                    seconds=request.app["JWT"]["JWT_EXP_DELTA_SECONDS"]
                ),
            }
            jwt_token = jwt.encode(
                payload,
                request.app["JWT"]["JWT_SECRET"],
                request.app["JWT"]["JWT_ALGORITHM"],
            )

            # store the token, will need it
            session["jwt_token"] = jwt_token
            session["user_id"] = username

            print("LOGIN", session)

            return web.json_response({"message": "success"}, status=200)

        else:
            raise Exception("Bad credentials")

    except Exception as _e:
        print(f"Got error: {str(_e)}")
        _err = traceback.format_exc()
        print(_err)
        return web.json_response(
            {"message": f"Failed to login user: {_err}"}, status=401
        )


@routes.get("/logout", name="logout")
async def logout(request):
    """
        Logout web user
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    session.invalidate()

    # redirect to login page
    location = request.app.router["login"].url_for()
    # location = '/login'
    raise web.HTTPFound(location=location)


@routes.get("/test")
@auth_required
async def handler_test(request):
    return web.json_response({"message": "test ok."}, status=200)


@routes.get("/test_wrapper")
@login_required
async def wrapper_handler_test(request):
    return web.json_response({"message": "test ok."}, status=200)


@routes.get("/", name="root")
@login_required
async def root_handler(request):
    """
        Serve home page for the browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    context = {"logo": config["server"]["logo"], "user": session["user_id"]}
    response = aiohttp_jinja2.render_template("template-root.html", request, context)
    # response.headers['Content-Language'] = 'ru'
    return response


""" manage users: API """


@routes.get("/users")
@login_required
async def manage_users(request):
    """
        Serve users page for the browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    # only admin can access this
    if session["user_id"] == config["server"]["admin_username"]:
        users = (
            await request.app["mongo"]
            .users.find({}, {"password": 0})
            .to_list(length=1000)
        )
        # print(users)

        context = {
            "logo": config["server"]["logo"],
            "user": session["user_id"],
            "users": users,
        }
        response = aiohttp_jinja2.render_template(
            "template-users.html", request, context
        )
        return response

    else:
        return web.json_response({"message": "403 Forbidden"}, status=403)


@routes.put("/users")
@login_required
async def add_user(request):
    """
        Add new user to DB
    :return:
    """
    # get session:
    session = await get_session(request)

    _data = await request.json()
    # print(_data)

    if session["user_id"] == config["server"]["admin_username"]:
        try:
            username = _data["user"] if "user" in _data else None
            password = _data["password"] if "password" in _data else None
            permissions = _data["permissions"] if "permissions" in _data else "{}"

            if len(username) == 0 or len(password) == 0:
                return web.json_response(
                    {"message": "username and password must be set"}, status=500
                )

            if len(permissions) == 0:
                permissions = "{}"

            # add user to coll_usr collection:
            await request.app["mongo"].users.insert_one(
                {
                    "_id": username,
                    "password": generate_password_hash(password),
                    "permissions": literal_eval(str(permissions)),
                    "last_modified": datetime.datetime.now(),
                }
            )

            return web.json_response({"message": "success"}, status=200)

        except Exception as _e:
            print(f"Got error: {str(_e)}")
            _err = traceback.format_exc()
            print(_err)
            return web.json_response(
                {"message": f"Failed to add user: {_err}"}, status=500
            )
    else:
        return web.json_response({"message": "403 Forbidden"}, status=403)


@routes.delete("/users")
@login_required
async def remove_user(request):
    """
        Remove user from DB
    :return:
    """
    # get session:
    session = await get_session(request)

    _data = await request.json()
    # print(_data)

    if session["user_id"] == config["server"]["admin_username"]:
        try:
            # get username from request
            username = _data["user"] if "user" in _data else None
            if username == config["server"]["admin_username"]:
                return web.json_response(
                    {"message": "Cannot remove the superuser!"}, status=500
                )

            # try to remove the user:
            await request.app["mongo"].users.delete_one({"_id": username})

            return web.json_response({"message": "success"}, status=200)

        except Exception as _e:
            print(f"Got error: {str(_e)}")
            _err = traceback.format_exc()
            print(_err)
            return web.json_response(
                {"message": f"Failed to remove user: {_err}"}, status=500
            )
    else:
        return web.json_response({"message": "403 Forbidden"}, status=403)


@routes.post("/users")
@login_required
async def edit_user(request):
    """
        Edit user info
    :return:
    """
    # get session:
    session = await get_session(request)

    _data = await request.json()
    # print(_data)

    if session["user_id"] == config["server"]["admin_username"]:
        try:
            _id = _data["_user"] if "_user" in _data else None
            username = _data["edit-user"] if "edit-user" in _data else None
            password = _data["edit-password"] if "edit-password" in _data else None
            # permissions = _data['edit-permissions'] if 'edit-permissions' in _data else '{}'

            if (
                _id == config["server"]["admin_username"]
                and username != config["server"]["admin_username"]
            ):
                return web.json_response(
                    {"message": "Cannot change the admin username!"}, status=500
                )

            if len(username) == 0:
                return web.json_response(
                    {"message": "username must be set"}, status=500
                )

            # change username:
            if _id != username:
                select = await request.app["mongo"].users.find_one({"_id": _id})
                select["_id"] = username
                await request.app["mongo"].users.insert_one(select)
                await request.app["mongo"].users.delete_one({"_id": _id})

            # change password:
            if len(password) != 0:
                await request.app["mongo"].users.update_one(
                    {"_id": username},
                    {
                        "$set": {"password": generate_password_hash(password)},
                        "$currentDate": {"last_modified": True},
                    },
                )

            # change permissions:
            # if len(permissions) != 0:
            #     select = await request.app['mongo'].users.find_one({'_id': username}, {'_id': 0, 'permissions': 1})
            #     # print(select)
            #     # print(permissions)
            #     _p = literal_eval(str(permissions))
            #     # print(_p)
            #     if str(permissions) != str(select['permissions']):
            #         result = await request.app['mongo'].users.update_one(
            #             {'_id': _id},
            #             {
            #                 '$set': {
            #                     'permissions': _p
            #                 },
            #                 '$currentDate': {'last_modified': True}
            #             }
            #         )

            return web.json_response({"message": "success"}, status=200)

        except Exception as _e:
            print(f"Got error: {str(_e)}")
            _err = traceback.format_exc()
            print(_err)
            return web.json_response(
                {"message": f"Failed to remove user: {_err}"}, status=500
            )
    else:
        return web.json_response({"message": "403 Forbidden"}, status=403)


""" manage user programs: API """


@routes.get("/programs")
@login_required
async def programs_get_handler(request):
    """
        Serve programs page for the browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    frmt = request.query.get("format", "web")

    programs = await request.app["mongo"].programs.find({}).to_list(length=1000)
    # print(programs)

    # count objects:
    for program in programs:
        num_objects = await request.app["mongo"].sources.count_documents(
            {"zvm_program_id": program["_id"]}
        )
        program["num_objects"] = num_objects

    if frmt == "web":
        context = {
            "logo": config["server"]["logo"],
            "user": session["user_id"],
            "programs": programs,
        }
        response = aiohttp_jinja2.render_template(
            "template-programs.html", request, context
        )
        return response

    elif frmt == "json":
        return web.json_response(programs, status=200, dumps=dumps)


@routes.put("/programs")
@login_required
async def programs_put_handler(request):
    """
        Add new program to DB
    :return:
    """
    # get session:
    await get_session(request)

    _data = await request.json()
    # print(_data)

    try:
        program_name = _data["program_name"] if "program_name" in _data else None
        program_description = (
            _data["program_description"] if "program_description" in _data else None
        )

        if len(program_name) == 0 or len(program_description) == 0:
            return web.json_response(
                {"message": "program name and description must be set"}, status=500
            )

        # get number of programs
        num_programs = await request.app["mongo"].programs.count_documents({})

        # add program to programs collection:
        doc = {
            "_id": int(num_programs + 1),
            "name": program_name,
            "description": program_description,
            "last_modified": datetime.datetime.now(),
        }
        await request.app["mongo"].programs.insert_one(doc)

        return web.json_response(
            {"message": "success", "result": doc}, status=200, dumps=dumps
        )

    except Exception as _e:
        print(f"Got error: {str(_e)}")
        _err = traceback.format_exc()
        print(_err)
        return web.json_response({"message": f"Failed to add user: {_err}"}, status=500)


# todo: /programs POST and DELETE


""" query API"""


regex = dict()
regex["collection_main"] = re.compile(r"db\[['\"](.*?)['\"]\]")
regex["aggregate"] = re.compile(r"aggregate\((\[(?s:.*)\])")


def parse_query(task, save: bool = False):
    # save auxiliary stuff
    kwargs = task["kwargs"] if "kwargs" in task else {}

    # reduce!
    task_reduced = {"user": task["user"], "query": {}, "kwargs": kwargs}

    # fixme: this is for testing api from cl
    # if '_id' not in task_reduced['kwargs']:
    #     task_reduced['kwargs']['_id'] = ''.join(random.choices(string.ascii_letters + string.digits, k=32))

    if task["query_type"] == "general_search":
        # specify task type:
        task_reduced["query_type"] = "general_search"
        # nothing dubious to start with?
        if task["user"] != config["server"]["admin_username"]:
            go_on = (
                True
                in [
                    s in str(task["query"])
                    for s in [
                        ".aggregate(",
                        ".map_reduce(",
                        ".distinct(",
                        ".estimated_document_count(",
                        ".count_documents(",
                        ".index_information(",
                        ".find_one(",
                        ".find(",
                    ]
                ]
                and True
                not in [
                    s in str(task["query"])
                    for s in [
                        "import",
                        "pymongo.",
                        "shutil.",
                        "command(",
                        "bulk_write(",
                        "exec(",
                        "spawn(",
                        "subprocess(",
                        "call(",
                        "insert(",
                        "update(",
                        "delete(",
                        "create_index(",
                        "create_collection(",
                        "run(",
                        "popen(",
                        "Popen(",
                    ]
                ]
                and str(task["query"]).strip()[0] not in ('"', "'", "[", "(", "{", "\\")
            )
        else:
            go_on = True

        # TODO: check access permissions:
        # TODO: for now, only check on admin stuff
        if task["user"] != config["server"]["admin_username"]:
            prohibited_collections = ("users", "stats", "queries")

            # get the main collection that is being queried:
            main_collection = (
                regex["collection_main"].search(str(task["query"])).group(1)
            )
            # print(main_collection)

            if main_collection in prohibited_collections:
                go_on = False

            # aggregating?
            if ".aggregate(" in str(task["query"]):
                pipeline = literal_eval(
                    regex["aggregate"].search(str(task["query"])).group(1)
                )
                # pipeline = literal_eval(self.regex['aggregate'].search(str(task['query'])).group(1))
                lookups = [
                    _ip for (_ip, _pp) in enumerate(pipeline) if "$lookup" in _pp
                ]
                for _l in lookups:
                    if pipeline[_l]["$lookup"]["from"] in prohibited_collections:
                        go_on = False

        if go_on:
            task_reduced["query"] = task["query"]
        else:
            raise Exception("Atata!")

    elif task["query_type"] == "find":
        # specify task type:
        task_reduced["query_type"] = "find"

        go_on = True

        if task["user"] != config["server"]["admin_username"]:
            prohibited_collections = ("users", "stats", "queries")
            if str(task["query"]["catalog"]) in prohibited_collections:
                go_on = False

        if go_on:
            task_reduced["query"]["catalog"] = task["query"]["catalog"]

            # construct filter
            _filter = task["query"]["filter"]
            if isinstance(_filter, str):
                # passed string? evaluate:
                catalog_filter = literal_eval(_filter.strip())
            elif isinstance(_filter, dict):
                # passed dict?
                catalog_filter = _filter
            else:
                raise ValueError("Unsupported filter specification")

            task_reduced["query"]["filter"] = catalog_filter

            # construct projection
            if "projection" in task["query"]:
                _projection = task["query"]["projection"]
                if isinstance(_projection, str):
                    # passed string? evaluate:
                    catalog_projection = literal_eval(_projection.strip())
                elif isinstance(_filter, dict):
                    # passed dict?
                    catalog_projection = _projection
                else:
                    raise ValueError("Unsupported projection specification")
            else:
                catalog_projection = dict()

            task_reduced["query"]["projection"] = catalog_projection

        else:
            raise Exception("Atata!")

    elif task["query_type"] == "find_one":
        # specify task type:
        task_reduced["query_type"] = "find_one"

        go_on = True

        if task["user"] != config["server"]["admin_username"]:
            prohibited_collections = ("users", "stats", "queries")
            if str(task["query"]["catalog"]) in prohibited_collections:
                go_on = False

        if go_on:
            task_reduced["query"]["catalog"] = task["query"]["catalog"]

            # construct filter
            _filter = task["query"]["filter"]
            if isinstance(_filter, str):
                # passed string? evaluate:
                catalog_filter = literal_eval(_filter.strip())
            elif isinstance(_filter, dict):
                # passed dict?
                catalog_filter = _filter
            else:
                raise ValueError("Unsupported filter specification")

            task_reduced["query"]["filter"] = catalog_filter

        else:
            raise Exception("Atata!")

    elif task["query_type"] == "count_documents":
        # specify task type:
        task_reduced["query_type"] = "count_documents"

        go_on = True

        if task["user"] != config["server"]["admin_username"]:
            prohibited_collections = ("users", "stats", "queries")
            if str(task["query"]["catalog"]) in prohibited_collections:
                go_on = False

        if go_on:
            task_reduced["query"]["catalog"] = task["query"]["catalog"]

            # construct filter
            _filter = task["query"]["filter"]
            if isinstance(_filter, str):
                # passed string? evaluate:
                catalog_filter = literal_eval(_filter.strip())
            elif isinstance(_filter, dict):
                # passed dict?
                catalog_filter = _filter
            else:
                raise ValueError("Unsupported filter specification")

            task_reduced["query"]["filter"] = catalog_filter

        else:
            raise Exception("Atata!")

    elif task["query_type"] == "aggregate":
        # specify task type:
        task_reduced["query_type"] = "aggregate"

        go_on = True

        if task["user"] != config["server"]["admin_username"]:
            prohibited_collections = ("users", "stats", "queries")
            if str(task["query"]["catalog"]) in prohibited_collections:
                go_on = False

        if go_on:
            task_reduced["query"]["catalog"] = task["query"]["catalog"]

            # construct pipeline
            _pipeline = task["query"]["pipeline"]
            if isinstance(_pipeline, str):
                # passed string? evaluate:
                catalog_pipeline = literal_eval(_pipeline.strip())
            elif isinstance(_pipeline, list) or isinstance(_pipeline, tuple):
                # passed dict?
                catalog_pipeline = _pipeline
            else:
                raise ValueError("Unsupported pipeline specification")

            task_reduced["query"]["pipeline"] = catalog_pipeline

        else:
            raise Exception("Atata!")

    elif task["query_type"] == "cone_search":
        # specify task type:
        task_reduced["query_type"] = "cone_search"
        # cone search radius:
        cone_search_radius = float(task["object_coordinates"]["cone_search_radius"])
        # convert to rad:
        if task["object_coordinates"]["cone_search_unit"] == "arcsec":
            cone_search_radius *= np.pi / 180.0 / 3600.0
        elif task["object_coordinates"]["cone_search_unit"] == "arcmin":
            cone_search_radius *= np.pi / 180.0 / 60.0
        elif task["object_coordinates"]["cone_search_unit"] == "deg":
            cone_search_radius *= np.pi / 180.0
        elif task["object_coordinates"]["cone_search_unit"] == "rad":
            cone_search_radius *= 1
        else:
            raise Exception(
                "Unknown cone search unit. Must be in [deg, rad, arcsec, arcmin]"
            )

        if isinstance(task["object_coordinates"]["radec"], str):
            radec = task["object_coordinates"]["radec"].strip()

            # comb radecs for single sources as per Tom's request:
            if radec[0] not in ("[", "(", "{"):
                ra, dec = radec.split()
                if ("s" in radec) or (":" in radec):
                    radec = f"[('{ra}', '{dec}')]"
                else:
                    radec = f"[({ra}, {dec})]"

            # print(task['object_coordinates']['radec'])
            objects = literal_eval(radec)
            # print(type(objects), isinstance(objects, dict), isinstance(objects, list))
        elif (
            isinstance(task["object_coordinates"]["radec"], list)
            or isinstance(task["object_coordinates"]["radec"], tuple)
            or isinstance(task["object_coordinates"]["radec"], dict)
        ):
            objects = task["object_coordinates"]["radec"]
        else:
            raise Exception(
                "Unknown cone search unit. Must be in [deg, rad, arcsec, arcmin]"
            )

        # this could either be list/tuple [(ra1, dec1), (ra2, dec2), ..] or dict {'name': (ra1, dec1), ...}
        if isinstance(objects, list) or isinstance(objects, tuple):
            object_coordinates = objects
            object_names = [str(obj_crd) for obj_crd in object_coordinates]
        elif isinstance(objects, dict):
            object_names, object_coordinates = zip(*objects.items())
            object_names = list(map(str, object_names))
        else:
            raise ValueError("Unsupported object coordinates specs")

        # print(object_names, object_coordinates)

        for catalog in task["catalogs"]:
            # TODO: check that not trying to query what's not allowed!
            task_reduced["query"][catalog] = dict()
            # parse catalog query:
            # construct filter
            _filter = task["catalogs"][catalog]["filter"]
            if isinstance(_filter, str):
                # passed string? evaluate:
                catalog_query = literal_eval(_filter.strip())
            elif isinstance(_filter, dict):
                # passed dict?
                catalog_query = _filter
            else:
                raise ValueError("Unsupported filter specification")

            # construct projection
            _projection = task["catalogs"][catalog]["projection"]
            if isinstance(_projection, str):
                # passed string? evaluate:
                catalog_projection = literal_eval(_projection.strip())
            elif isinstance(_filter, dict):
                # passed dict?
                catalog_projection = _projection
            else:
                raise ValueError("Unsupported projection specification")

            # parse coordinate list

            if isinstance(_projection, str):
                # passed string? evaluate:
                catalog_projection = literal_eval(_projection.strip())
            elif isinstance(_filter, dict):
                # passed dict?
                catalog_projection = _projection

            for oi, obj_crd in enumerate(object_coordinates):
                # convert ra/dec into GeoJSON-friendly format
                # print(obj_crd)
                _ra, _dec = radec_str2geojson(*obj_crd)
                # print(str(obj_crd), _ra, _dec)
                object_position_query = dict()
                object_position_query["coordinates.radec_geojson"] = {
                    "$geoWithin": {"$centerSphere": [[_ra, _dec], cone_search_radius]}
                }
                # use stringified object coordinates as dict keys and merge dicts with cat/obj queries:
                task_reduced["query"][catalog][object_names[oi]] = (
                    {**object_position_query, **catalog_query},
                    {**catalog_projection},
                )

    elif task["query_type"] == "info":

        # specify task type:
        task_reduced["query_type"] = "info"
        task_reduced["query"] = task["query"]

    if save:
        # print(task_reduced)
        # task_hashable = dumps(task)
        task_hashable = dumps(task_reduced)
        # compute hash for task. this is used as key in DB
        task_hash = compute_hash(task_hashable)

        # print({'user': task['user'], 'task_id': task_hash})

        # mark as enqueued in DB:
        t_stamp = utc_now()
        if "query_expiration_interval" not in kwargs:
            # default expiration interval:
            t_expires = t_stamp + datetime.timedelta(
                days=int(config["misc"]["query_expiration_interval"])
            )
        else:
            # custom expiration interval:
            t_expires = t_stamp + datetime.timedelta(
                days=int(kwargs["query_expiration_interval"])
            )

        # dump task_hashable to file, as potentially too big to store in mongo
        # save task:
        user_tmp_path = os.path.join(config["path"]["path_queries"], task["user"])
        # print(user_tmp_path)
        # mkdir if necessary
        if not os.path.exists(user_tmp_path):
            os.makedirs(user_tmp_path)
        task_file = os.path.join(user_tmp_path, f"{task_hash}.task.json")

        with open(task_file, "w") as f_task_file:
            f_task_file.write(dumps(task))

        task_doc = {
            "task_id": task_hash,
            "user": task["user"],
            "task": task_file,
            "result": None,
            "status": "enqueued",
            "created": t_stamp,
            "expires": t_expires,
            "last_modified": t_stamp,
        }

        return task_hash, task_reduced, task_doc

    else:
        return "", task_reduced, {}


async def execute_query(mongo, task_hash, task_reduced, task_doc, save: bool = False):

    db = mongo

    if save:
        # mark query as enqueued:
        await db.queries.insert_one(task_doc)

    result = dict()
    query_result = dict()

    query = task_reduced

    result["user"] = query["user"]
    result["kwargs"] = query["kwargs"] if "kwargs" in query else {}

    # by default, long-running queries will be killed after config['misc']['max_time_ms'] ms
    max_time_ms = (
        int(query["kwargs"]["max_time_ms"])
        if "max_time_ms" in query["kwargs"]
        else int(config["misc"]["max_time_ms"])
    )
    assert max_time_ms >= 1, "bad max_time_ms, must be int>=1"

    try:

        # cone search:
        if query["query_type"] == "cone_search":

            known_kwargs = ("skip", "hint", "limit", "sort")
            kwargs = {
                kk: vv for kk, vv in query["kwargs"].items() if kk in known_kwargs
            }
            kwargs["comment"] = str(query["user"])

            # iterate over catalogs as they represent
            for catalog in query["query"]:
                query_result[catalog] = dict()
                # iterate over objects:
                for obj in query["query"][catalog]:
                    # project?
                    if len(query["query"][catalog][obj][1]) > 0:
                        _select = db[catalog].find(
                            query["query"][catalog][obj][0],
                            query["query"][catalog][obj][1],
                            max_time_ms=max_time_ms,
                            **kwargs,
                        )
                    # return the whole documents by default
                    else:
                        _select = db[catalog].find(
                            query["query"][catalog][obj][0],
                            max_time_ms=max_time_ms,
                            **kwargs,
                        )
                    # mongodb does not allow having dots in field names -> replace with underscores
                    query_result[catalog][
                        obj.replace(".", "_")
                    ] = await _select.to_list(length=None)

        # convenience general search subtypes:
        elif query["query_type"] == "find":
            # print(query)

            known_kwargs = ("skip", "hint", "limit", "sort")
            kwargs = {
                kk: vv for kk, vv in query["kwargs"].items() if kk in known_kwargs
            }
            kwargs["comment"] = str(query["user"])

            # project?
            if len(query["query"]["projection"]) > 0:

                _select = db[query["query"]["catalog"]].find(
                    query["query"]["filter"],
                    query["query"]["projection"],
                    max_time_ms=max_time_ms,
                    **kwargs,
                )
            # return the whole documents by default
            else:
                _select = db[query["query"]["catalog"]].find(
                    query["query"]["filter"], max_time_ms=max_time_ms, **kwargs
                )

            if (
                isinstance(_select, int)
                or isinstance(_select, float)
                or isinstance(_select, tuple)
                or isinstance(_select, list)
                or isinstance(_select, dict)
                or (_select is None)
            ):
                query_result["query_result"] = _select
            else:
                query_result["query_result"] = await _select.to_list(length=None)

        elif query["query_type"] == "find_one":
            # print(query)

            known_kwargs = ("skip", "hint", "limit", "sort")
            kwargs = {
                kk: vv for kk, vv in query["kwargs"].items() if kk in known_kwargs
            }
            kwargs["comment"] = str(query["user"])

            _select = db[query["query"]["catalog"]].find_one(
                query["query"]["filter"], max_time_ms=max_time_ms
            )

            query_result["query_result"] = await _select

        elif query["query_type"] == "count_documents":
            # print(query)

            known_kwargs = ("skip", "hint", "limit")
            kwargs = {
                kk: vv for kk, vv in query["kwargs"].items() if kk in known_kwargs
            }
            kwargs["comment"] = str(query["user"])

            _select = db[query["query"]["catalog"]].count_documents(
                query["query"]["filter"], maxTimeMS=max_time_ms
            )

            query_result["query_result"] = await _select

        elif query["query_type"] == "aggregate":
            # print(query)

            known_kwargs = ("allowDiskUse", "maxTimeMS", "batchSize")
            kwargs = {
                kk: vv for kk, vv in query["kwargs"].items() if kk in known_kwargs
            }
            kwargs["comment"] = str(query["user"])

            _select = db[query["query"]["catalog"]].aggregate(
                query["query"]["pipeline"], allowDiskUse=True, maxTimeMS=max_time_ms
            )

            query_result["query_result"] = await _select.to_list(length=None)

        elif query["query_type"] == "general_search":
            # just evaluate. I know that's dangerous, but...
            qq = bytes(query["query"], "utf-8").decode("unicode_escape")

            _select = eval(qq)
            # _select = eval(query['query'])
            # _select = literal_eval(qq)

            if (
                (".find_one(" in qq)
                or (".count_documents(" in qq)
                or (".estimated_document_count(" in qq)
                or (".index_information(" in qq)
                or (".distinct(" in qq)
            ):
                _select = await _select

            # make it look like json
            # print(list(_select))
            if (
                isinstance(_select, int)
                or isinstance(_select, float)
                or isinstance(_select, tuple)
                or isinstance(_select, list)
                or isinstance(_select, dict)
                or (_select is None)
            ):
                query_result["query_result"] = _select
            else:
                query_result["query_result"] = await _select.to_list(length=None)

        elif query["query_type"] == "info":
            # collection/catalog info

            if query["query"]["command"] == "catalog_names":

                # get available catalog names
                catalogs = await db.list_collection_names()
                # exclude system collections and collections without a 2dsphere index
                catalogs_system = (
                    config["database"]["collection_users"],
                    config["database"]["collection_queries"],
                    config["database"]["collection_stats"],
                )

                query_result["query_result"] = [
                    c for c in sorted(catalogs)[::-1] if c not in catalogs_system
                ]

            elif query["query"]["command"] == "catalog_info":

                catalog = query["query"]["catalog"]

                stats = await db.command("collstats", catalog)

                query_result["query_result"] = stats

            elif query["query"]["command"] == "index_info":

                catalog = query["query"]["catalog"]

                stats = await db[catalog].index_information()

                query_result["query_result"] = stats

            elif query["query"]["command"] == "db_info":

                stats = await db.command("dbstats")
                query_result["query_result"] = stats

        # success!
        result["status"] = "done"

        if not save:
            # dump result back
            result["result_data"] = query_result

        else:
            # save task result:
            user_tmp_path = os.path.join(config["path"]["path_queries"], query["user"])
            # print(user_tmp_path)
            # mkdir if necessary
            if not os.path.exists(user_tmp_path):
                os.makedirs(user_tmp_path)
            task_result_file = os.path.join(user_tmp_path, f"{task_hash}.result.json")

            # save location in db:
            result["result"] = task_result_file

            async with aiofiles.open(task_result_file, "w") as f_task_result_file:
                task_result = dumps(query_result)
                await f_task_result_file.write(task_result)

        # print(task_hash, result)

        # db book-keeping:
        if save:
            # mark query as done:
            await db.queries.update_one(
                {"user": query["user"], "task_id": task_hash},
                {
                    "$set": {
                        "status": result["status"],
                        "last_modified": utc_now(),
                        "result": result["result"],
                    }
                },
            )

        # return task_hash, dumps(result)
        return task_hash, result

    except Exception as e:
        print(f"Got error: {str(e)}")
        _err = traceback.format_exc()
        print(_err)

        # book-keeping:
        if save:
            # save task result with error message:
            user_tmp_path = os.path.join(config["path"]["path_queries"], query["user"])
            # print(user_tmp_path)
            # mkdir if necessary
            if not os.path.exists(user_tmp_path):
                os.makedirs(user_tmp_path)
            task_result_file = os.path.join(user_tmp_path, f"{task_hash}.result.json")

            # save location in db:
            # result['user'] = query['user']
            result["status"] = "failed"

            query_result = dict()
            query_result["msg"] = _err

            async with aiofiles.open(task_result_file, "w") as f_task_result_file:
                task_result = dumps(query_result)
                await f_task_result_file.write(task_result)

            # mark query as failed:
            await db.queries.update_one(
                {"user": query["user"], "task_id": task_hash},
                {
                    "$set": {
                        "status": result["status"],
                        "last_modified": utc_now(),
                        "result": None,
                    }
                },
            )

        else:
            result["status"] = "failed"
            result["msg"] = _err

            return task_hash, result

        raise Exception("Query failed")


@routes.put("/query")
@login_required
async def query_handler(request):
    """
        Query own db, return json
    :param request:
    :return:
    """
    user = request.get("user", None)
    # try session if None:
    if user is None:
        session = await get_session(request)
        user = session["user_id"]

    try:
        _query = await request.json()
    except Exception as _e:
        print(f"Cannot extract json() from request, trying post(): {str(_e)}")
        _query = await request.post()
    # print(_query)

    # parse and execute query awaiting the result

    try:
        # parse query
        # known_query_types = ('cone_search', 'general_search')
        # add separate "convenience" query types for the most in-demand cases:
        known_query_types = (
            "cone_search",
            "general_search",
            "find",
            "find_one",
            "aggregate",
            "count_documents",
            "info",
        )

        assert (
            _query["query_type"] in known_query_types
        ), f'query_type {_query["query_type"]} not in {str(known_query_types)}'

        _query["user"] = user
        save = False  # query scheduling is disabled as unnecessary for the Variable Marshal (compare to Kowalski)

        # tic = time.time()
        task_hash, task_reduced, task_doc = parse_query(_query, save=save)
        # toc = time.time()
        # print(f'parsing task took {toc-tic} seconds')
        # print(task_hash, task_reduced, task_doc)

        # execute query:
        task_hash, result = await execute_query(
            request.app["mongo"], task_hash, task_reduced, task_doc, save
        )

        # print(result)

        return web.json_response(
            {"message": "success", "result": result}, status=200, dumps=dumps
        )

    except Exception as _e:
        print(f"Got error: {str(_e)}")
        _err = traceback.format_exc()
        print(_err)
        return web.json_response({"message": f"failure: {_err}"}, status=500)


""" Label sources """


@routes.get("/label")
@login_required
async def label_get_handler(request):
    """
        Labeling interface
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)
    user = session["user_id"]

    try:
        users = (
            await request.app["mongo"].users.find({}, {"_id": 1}).to_list(length=None)
        )
        users = sorted([uu["_id"] for uu in users])

        programs = (
            await request.app["mongo"]
            .programs.find({}, {"_id": 1})
            .to_list(length=None)
        )
        programs = sorted([pp["_id"] for pp in programs])

        classes = config["classifications"]

        descriptions = config["label_descriptions"]

        _r = request.rel_url.query
        zvm_program_id = _r.get("zvm_program_id", None)
        filt = _r.get("filter", None)
        if filt and len(filt) > 0:
            try:
                filt = literal_eval(filt)
                if not isinstance(filt, Mapping):
                    filt = dict()
            except Exception:
                filt = dict()
        else:
            filt = dict()

        number = _r.get("number", None)
        rand = _r.get("random", False)
        unlabeled = _r.get("unlabeled", False)

        sources = []
        if zvm_program_id and number:
            filt = {"zvm_program_id": int(zvm_program_id), **filt}

            if unlabeled:
                # filt = {**filt, **{'$or': [{'labels': {'$size': 0}},
                #                            {'labels.user': {'$ne': user}}]}}
                filt = {**filt, **{"labels.user": {"$ne": user}}}
            else:
                filt = {**filt, **{"labels.user": {"$eq": user}}}
            if not rand:
                # '$or': [{'labels.user': {'$exists': False}},
                #         {'labels.user': user}]
                sources = (
                    await request.app["mongo"]
                    .sources.find(
                        filt, {"xmatch.ZTF_alerts": 0, "history": 0, "spec.data": 0}
                    )
                    .limit(int(number))
                    .sort([("created", -1)])
                    .to_list(length=None)
                )
                # print(sources)
            else:
                # fixme: slow on large number of matches. create indices to speed up!
                pipeline = [
                    {"$match": filt},
                    {"$project": {"_id": 1}},
                    {"$sample": {"size": int(number)}},
                ]
                _select = request.app["mongo"].sources.aggregate(
                    pipeline, allowDiskUse=True, maxTimeMS=30000
                )

                source_ids = await _select.to_list(length=None)
                source_ids = [sid["_id"] for sid in source_ids]

                pipeline = [
                    {"$match": {"_id": {"$in": source_ids}}},
                    {
                        "$project": {
                            "xmatch.ZTF_alerts": 0,
                            "history": 0,
                            "spec.data": 0,
                        }
                    },
                ]
                _select = request.app["mongo"].sources.aggregate(
                    pipeline, allowDiskUse=True, maxTimeMS=30000
                )

                sources = await _select.to_list(length=None)

        # fixme: pop other people's labels. should Do this on mongodb's side
        for source in sources:
            labels = [
                lab for lab in source.get("labels", ()) if lab.get("user", None) == user
            ]
            source["labels"] = labels

        context = {
            "logo": config["server"]["logo"],
            "user": session["user_id"],
            "users": users,
            "programs": programs,
            "classes": classes,
            "descriptions": descriptions,
            "data": sources,
            "messages": [],
        }

        response = aiohttp_jinja2.render_template(
            "template-label.html", request, context
        )
        return response

    except Exception as _e:
        print(f"Error: {str(_e)}")
        _err = traceback.format_exc()
        print(_err)

        context = {
            "logo": config["server"]["logo"],
            "user": session["user_id"],
            "users": [],
            "programs": [],
            "classes": [],
            "descriptions": [],
            "data": [],
            "messages": [
                [
                    f"Encountered error while loading sources: {str(_e)}. Reload the page!",
                    "danger",
                ]
            ],
        }

        response = aiohttp_jinja2.render_template(
            "template-label.html", request, context
        )
        return response


# @routes.post('/label')
# @login_required
# async def label_post_handler(request):
#     """
#         Save labels
#     :param request:
#     :return:
#     """
#     # get session:
#     session = await get_session(request)
#
#     try:
#         _r = await request.json()
#     except Exception as _e:
#         print(f'Cannot extract json() from request, trying post(): {str(_e)}')
#         # _err = traceback.format_exc()
#         # print(_err)
#         _r = await request.post()
#     print(_r)
#
#     try:
#         # todo: save
#         return web.json_response({'message': 'success'}, status=200, dumps=dumps)
#
#     except Exception as _e:
#
#         return web.json_response({'message': str(_e)}, status=500)


""" sources API """


@routes.get("/sources")
@login_required
async def sources_get_handler(request):
    """
        Serve saved sources page for the browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    try:

        # todo: display light curves? for now, omit the actual data

        # get last 50 added sources
        # sources = await request.app['mongo'].sources.find({},
        #                                                   {'_id': 1,
        #                                                    'ra': 1,
        #                                                    'dec': 1,
        #                                                    'p': 1,
        #                                                    'source_type': 1,
        #                                                    'created': 1}).limit(10).sort({'created': -1}).to_list(
        #     length=None)
        sources = (
            await request.app["mongo"]
            .sources.find({}, {"coordinates": 0, "spec.data": 0, "lc.data": 0})
            .limit(50)
            .sort([("created", -1)])
            .to_list(length=None)
        )

        users = (
            await request.app["mongo"].users.find({}, {"_id": 1}).to_list(length=None)
        )
        users = sorted([uu["_id"] for uu in users])

        programs = (
            await request.app["mongo"]
            .programs.find({}, {"_id": 1})
            .to_list(length=None)
        )
        programs = sorted([pp["_id"] for pp in programs])

        context = {
            "logo": config["server"]["logo"],
            "user": session["user_id"],
            "users": users,
            "programs": programs,
            "data": sources,
            "messages": [["Displaying latest saved sources", "info"]],
        }

        response = aiohttp_jinja2.render_template(
            "template-sources.html", request, context
        )
        return response

    except Exception as _e:
        print(f"Error: {str(_e)}")

        context = {
            "logo": config["server"]["logo"],
            "user": session["user_id"],
            "users": [],
            "programs": [],
            "data": [],
            "messages": [
                [
                    f"Encountered error while loading sources: {str(_e)}. Reload the page!",
                    "danger",
                ]
            ],
        }

        response = aiohttp_jinja2.render_template(
            "template-sources.html", request, context
        )
        return response


@routes.post("/sources")
@login_required
async def sources_post_handler(request):
    """
        Process query to own db from browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    try:
        _query = await request.json()
    except Exception as _e:
        print(f"Cannot extract json() from request, trying post(): {str(_e)}")
        # _err = traceback.format_exc()
        # print(_err)
        _query = await request.post()
    # print(_query)

    try:
        # parse query
        q = dict()

        # filter set?
        if len(_query["filter"]) > 2:
            # construct filter
            _filter = _query["filter"]
            if isinstance(_filter, str):
                # passed string? evaluate:
                _filter = literal_eval(_filter.strip())
            elif isinstance(_filter, dict):
                # passed dict?
                _filter = _filter
            else:
                raise ValueError("Unsupported filter specification")

            q = {**q, **_filter}

        # cone search?
        if len(_query["cone_search_radius"]) > 0 and len(_query["radec"]) > 8:
            cone_search_radius = float(_query["cone_search_radius"])
            # convert to rad:
            if _query["cone_search_unit"] == "arcsec":
                cone_search_radius *= np.pi / 180.0 / 3600.0
            elif _query["cone_search_unit"] == "arcmin":
                cone_search_radius *= np.pi / 180.0 / 60.0
            elif _query["cone_search_unit"] == "deg":
                cone_search_radius *= np.pi / 180.0
            elif _query["cone_search_unit"] == "rad":
                cone_search_radius *= 1
            else:
                raise Exception(
                    "Unknown cone search unit. Must be in [deg, rad, arcsec, arcmin]"
                )

            # parse coordinate list

            # comb radecs for single sources as per Tom's request:
            radec = _query["radec"].strip()
            if radec[0] not in ("[", "(", "{"):
                ra, dec = radec.split()
                if ("s" in radec) or (":" in radec):
                    radec = f"[('{ra}', '{dec}')]"
                else:
                    radec = f"[({ra}, {dec})]"

            # print(task['object_coordinates']['radec'])
            objects = literal_eval(radec)
            # print(type(objects), isinstance(objects, dict), isinstance(objects, list))

            # this could either be list [(ra1, dec1), (ra2, dec2), ..] or dict {'name': (ra1, dec1), ...}
            if isinstance(objects, list):
                object_coordinates = objects
                object_names = [str(obj_crd) for obj_crd in object_coordinates]
            elif isinstance(objects, dict):
                object_names, object_coordinates = zip(*objects.items())
                object_names = list(map(str, object_names))
            else:
                raise ValueError("Unsupported type of object coordinates")

            # print(object_names, object_coordinates)

            object_position_query = dict()
            object_position_query["$or"] = []

            for oi, obj_crd in enumerate(object_coordinates):
                # convert ra/dec into GeoJSON-friendly format
                # print(obj_crd)
                _ra, _dec = radec_str2geojson(*obj_crd)
                # print(str(obj_crd), _ra, _dec)

                object_position_query["$or"].append(
                    {
                        "coordinates.radec_geojson": {
                            "$geoWithin": {
                                "$centerSphere": [[_ra, _dec], cone_search_radius]
                            }
                        }
                    }
                )

            q = {**q, **object_position_query}
            q = {"$and": [q]}

        users = (
            await request.app["mongo"].users.find({}, {"_id": 1}).to_list(length=None)
        )
        users = sorted([uu["_id"] for uu in users])

        programs = (
            await request.app["mongo"]
            .programs.find({}, {"_id": 1})
            .to_list(length=None)
        )
        programs = sorted([pp["_id"] for pp in programs])

        # print(q)
        if len(q) == 0:
            context = {
                "logo": config["server"]["logo"],
                "user": session["user_id"],
                "data": [],
                "users": users,
                "programs": programs,
                "form": _query,
                "messages": [["Empty query", "danger"]],
            }

        else:

            sources = (
                await request.app["mongo"]
                .sources.find(
                    q, {"coordinates.radec_str": 0, "spec.data": 0, "lc.data": 0}
                )
                .sort([("created", -1)])
                .to_list(length=None)
            )

            context = {
                "logo": config["server"]["logo"],
                "user": session["user_id"],
                "data": sources,
                "users": users,
                "programs": programs,
                "form": _query,
            }

            if len(sources) == 0:
                context["messages"] = [["No sources found", "info"]]

        response = aiohttp_jinja2.render_template(
            "template-sources.html", request, context
        )
        return response

    except Exception as _e:

        print(f"Error: {str(_e)}")

        users = (
            await request.app["mongo"].users.find({}, {"_id": 1}).to_list(length=None)
        )
        users = sorted([uu["_id"] for uu in users])

        programs = (
            await request.app["mongo"]
            .programs.find({}, {"_id": 1})
            .to_list(length=None)
        )
        programs = sorted([pp["_id"] for pp in programs])

        context = {
            "logo": config["server"]["logo"],
            "user": session["user_id"],
            "data": [],
            "users": users,
            "programs": programs,
            "form": _query,
            "messages": [[f"Error: {str(_e)}", "danger"]],
        }

        response = aiohttp_jinja2.render_template(
            "template-sources.html", request, context
        )

        return response


@routes.get("/sources/{source_id}")
@login_required
async def source_get_handler(request):
    """
        Serve single saved source page for the browser or source json if ?format=json
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    _id = request.match_info["source_id"]

    source = await request.app["mongo"].sources.find_one({"_id": _id})
    source = loads(dumps(source))
    # print(source)

    frmt = request.query.get("format", "web")
    # print(frmt)

    if frmt == "json":
        return web.json_response(source, status=200, dumps=dumps)

    # for the web, reformat/compute data fields:
    # light curves
    bad_lc = []
    lc_color_indexes = dict()
    for ilc, lc in enumerate(source["lc"]):
        try:
            if lc["lc_type"] == "temporal":
                # convert to pandas dataframe and replace nans with zeros:
                df = pd.DataFrame(lc["data"]).fillna(0)

                # fixme?
                if "mjd" not in df:
                    df["mjd"] = df["hjd"] - 2400000.5
                if "hjd" not in df:
                    df["hjd"] = df["mjd"] + 2400000.5

                if "datetime" not in df:
                    df["datetime"] = df["mjd"].apply(lambda x: mjd_to_datetime(x))
                # strings for plotly:
                if "dt" not in df:
                    df["dt"] = df["datetime"].apply(
                        lambda x: x.strftime("%Y-%m-%d %H:%M:%S")
                    )

                df.sort_values(by=["mjd"], inplace=True)

                if "jd" not in df:
                    df["jd"] = df["mjd"] + 2400000.5

                # fractional days ago
                t_utc = datetime.datetime.utcnow()
                df["days_ago"] = df["datetime"].apply(
                    lambda x: (t_utc - x).total_seconds() / 86400.0
                )

                # print(df['programid'])

                # convert back to dict:
                lc["data"] = df.to_dict("records")

                # for field in ('mag', 'magerr', 'mag_llim', 'mag_ulim', 'mjd', 'hjd', 'jd', 'dt', 'days_ago'):
                #     lc[field] = df[field].values.tolist() if field in df else []

                # pre-process for plotly:
                # display color:
                lc_color_indexes[lc["filter"]] = (
                    lc_color_indexes[lc["filter"]] + 1
                    if lc["filter"] in lc_color_indexes
                    else 0
                )
                lc["color"] = lc_colors(lc["filter"], lc_color_indexes[lc["filter"]])

                lc__ = {
                    "lc_det": {
                        "dt": [],
                        "days_ago": [],
                        "jd": [],
                        "mjd": [],
                        "hjd": [],
                        "mag": [],
                        "magerr": [],
                    },
                    "lc_nodet_u": {
                        "dt": [],
                        "days_ago": [],
                        "jd": [],
                        "mjd": [],
                        "hjd": [],
                        "mag_ulim": [],
                    },
                    "lc_nodet_l": {
                        "dt": [],
                        "days_ago": [],
                        "jd": [],
                        "mjd": [],
                        "hjd": [],
                        "mag_llim": [],
                    },
                }
                for dp in lc["data"]:
                    if ("mag_ulim" in dp) and (dp["mag_ulim"] > 0.01):
                        for kk in ("dt", "days_ago", "jd", "mjd", "hjd", "mag_ulim"):
                            lc__["lc_nodet_u"][kk].append(dp[kk])
                    if ("mag_llim" in dp) and (dp["mag_llim"] > 0.01):
                        for kk in ("dt", "days_ago", "jd", "mjd", "hjd", "mag_llim"):
                            lc__["lc_nodet_l"][kk].append(dp[kk])
                    if ("mag" in dp) and (dp["mag"] > 0.01):
                        for kk in (
                            "dt",
                            "days_ago",
                            "jd",
                            "mjd",
                            "hjd",
                            "mag",
                            "magerr",
                        ):
                            lc__["lc_det"][kk].append(dp[kk])
                lc["data"] = lc__

        except Exception as e:
            print(str(e))
            _err = traceback.format_exc()
            print(_err)
            bad_lc.append(ilc)

    for blc in bad_lc[::-1]:
        source["lc"].pop(blc)

    # spectra
    bad_spec = []
    for ispec, spec in enumerate(source["spec"]):
        try:
            # convert to pandas dataframe and replace nans with zeros:
            df = pd.DataFrame(spec["data"]).fillna(0)
            # don't need this anymore:
            spec.pop("data", None)

            # todo: transform data if necessary, e.g. convert to same units etc
            # df['dt'] = df['mjd'].apply(lambda x: mjd_to_datetime(x).strftime('%Y-%m-%d %H:%M:%S'))

            df["wavelength"] = df["wavelength"].apply(lambda x: float(x))
            df.sort_values(by=["wavelength"], inplace=True)

            # print(df)

            for field in ("wavelength", "flux", "fluxerr"):
                spec[field] = df[field].values.tolist() if field in df else []
                spec[field] = [float(ee) for ee in spec[field]]

        except Exception as e:
            print(str(e))
            bad_spec.append(ispec)

    for bspec in bad_spec[::-1]:
        source["spec"].pop(bspec)

    # source types and tags:
    source_types = config["misc"]["source_types"]
    source_flags = config["misc"]["source_flags"]

    # get ZVM programs:
    programs = (
        await request.app["mongo"]
        .programs.find({}, {"last_modified": 0})
        .to_list(length=None)
    )

    context = {
        "logo": config["server"]["logo"],
        "user": session["user_id"],
        "source": source,
        "source_types": source_types,
        "source_flags": source_flags,
        "programs": programs,
        "cone_search_radius": config["kowalski"]["cross_match"]["cone_search_radius"],
        "cone_search_unit": config["kowalski"]["cross_match"]["cone_search_unit"],
    }
    response = aiohttp_jinja2.render_template("template-source.html", request, context)
    return response


@routes.get("/sources/{source_id}/images/ps1")
@login_required
async def source_cutout_get_handler(request):
    """
        Serve cutout image
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    _id = request.match_info["source_id"]

    source = (
        await request.app["mongo"]
        .sources.find({"_id": _id}, {"ra": 1, "dec": 1})
        .to_list(length=None)
    )
    source = loads(dumps(source[0]))

    try:
        ps1_url = get_rgb_ps_stamp_url(source["ra"], source["dec"], timeout=1.5)
        # print(ps1_url)
        async with aiohttp.ClientSession() as session:
            async with session.get(ps1_url) as resp:
                if resp.status == 200:
                    buff = io.BytesIO()
                    buff.write(await resp.read())
                    buff.seek(0)
                    return web.Response(body=buff, content_type="image/png")
    except Exception as e:
        print(e)

    return web.Response(body=io.BytesIO(), content_type="image/png")


@routes.get("/sources/{source_id}/images/hr")
@login_required
async def source_hr_get_handler(request):
    """
        Serve HR diagram for a source
    :param request:
    :return:
    """
    # get session:
    await get_session(request)

    _id = request.match_info["source_id"]

    catalogs = config["kowalski"]["catalogs_hr_diagram"]

    projection = {"ra": 1, "dec": 1}
    for catalog in catalogs:
        projection["xmatch." + catalog] = 1

    source = (
        await request.app["mongo"]
        .sources.find({"_id": _id}, projection)
        .to_list(length=None)
    )
    source = loads(dumps(source[0]))

    xmatches = []
    for catalog in catalogs:
        if catalog in source["xmatch"]:
            xmatches.extend(source["xmatch"][catalog])

    if len(xmatches) > 0:
        # pick the nearest match:
        ii = np.argmin(
            [
                great_circle_distance(
                    source["dec"] * np.pi / 180,
                    source["ra"] * np.pi / 180,
                    *radec_str2rad(*dd["coordinates"]["radec_str"])[::-1],
                )
                for dd in xmatches
            ]
        )

        xmatch = xmatches[ii]

        g = xmatch.get("phot_g_mean_mag", None)
        bp = xmatch.get("phot_bp_mean_mag", None)
        rp = xmatch.get("phot_rp_mean_mag", None)
        p = xmatch.get("parallax", None)

        x, y = np.nan, np.nan
        if g and bp and rp and p:
            x = bp - rp
            y = g + 5 * np.log10(p / 1000) + 5

        # we calculated x and y first, as the values might be NaNs
        # (that can happen if the parallax is negative for example)
        # which would caus the plot to not show anything
        if not math.isnan(x) and not math.isnan(y):
            try:
                img = plt.imread(current_dir + "/static/img/hr_plot.png")
                buff = io.BytesIO()
                fig = plt.figure(figsize=(4, 4), dpi=200)
                ax = fig.add_subplot(111)
                ax.plot(
                    x,
                    y,
                    "o",
                    markersize=8,
                    c="#f22f29",
                )
                ax.imshow(img, extent=[-1, 5, 17, -5])
                ax.set_aspect(1 / 4)
                ax.set_ylabel("G")
                ax.set_xlabel("BP-RP")
                ax.set_title(
                    f"BP-RP: {x:.4f}, G: {y:.4f}",
                    fontsize=10,
                    horizontalalignment="center",
                    color="black",
                )
                plt.tight_layout(pad=0, h_pad=0, w_pad=0)
                plt.savefig(buff, dpi=200, bbox_inches="tight")
                buff.seek(0)
                plt.close("all")
                return web.Response(body=buff, content_type="image/png")
            except Exception as e:
                print(e)

    img = plt.imread(current_dir + "/static/img/hr_plot.png")
    buff = io.BytesIO()
    fig = plt.figure(figsize=(4, 4), dpi=200)
    ax = fig.add_subplot(111)
    ax.imshow(img, extent=[-1, 5, 17, -5])
    ax.set_aspect(1 / 4)
    ax.set_ylabel("G")
    ax.set_xlabel("BP-RP")
    ax.set_title(
        f'No Match in {", ".join(catalogs)}\n (with all required properties)',
        fontsize=16,
        horizontalalignment="center",
        color="red",
    )
    plt.tight_layout(pad=0, h_pad=0, w_pad=0)
    plt.savefig(buff, dpi=200, bbox_inches="tight")
    buff.seek(0)
    plt.close("all")
    return web.Response(body=buff, content_type="image/png")


@routes.get("/api/images/hr")
@login_required
async def hr_get_handler(request):
    """
        Serve HR diagram for a source within sep arcseconds from (ra, dec) [deg, deg]
        Cone-search Gaia DR2 for that. Return empty if no good candidates are found
    :param request:
    :return:
    """
    # get session:
    await get_session(request)

    # GET params:
    _r = request.rel_url.query

    # crds:
    ra = _r.get("ra", None)
    dec = _r.get("dec", None)
    sep = _r.get("sep", 5)

    catalogs = config["kowalski"]["catalogs_hr_diagram"]

    if (ra is not None) and (dec is not None):
        ra = float(ra)
        dec = float(dec)

        kowalski_query_xmatch = {
            "query_type": "cone_search",
            "query": {
                "object_coordinates": {
                    "radec": {"source": (ra, dec)},
                    "cone_search_radius": sep,
                    "cone_search_unit": "arcsec",
                },
                "catalogs": {
                    catalog: {
                        "filter": {},
                        "projection": {
                            "_id": 1,
                            "coordinates.radec_str": 1,
                            "parallax": 1,
                            "parallax_error": 1,
                            "phot_g_mean_mag": 1,
                            "phot_bp_mean_mag": 1,
                            "phot_rp_mean_mag": 1,
                        },
                    }
                    for catalog in catalogs
                },
            },
        }

        resp = request.app["kowalski"].query(kowalski_query_xmatch)

        xmatch = []
        for instance_results in resp.values():
            for catalog_results in instance_results.get("data", {}).values():
                xmatch.extend(catalog_results.get("source", []))

        if len(xmatch) > 0:
            # pick the nearest match:
            ii = np.argmin(
                [
                    great_circle_distance(
                        dec * np.pi / 180,
                        ra * np.pi / 180,
                        *radec_str2rad(*dd["coordinates"]["radec_str"])[::-1],
                    )
                    for dd in xmatch
                ]
            )

            xmatch = xmatch[ii]

            g = xmatch.get("phot_g_mean_mag", None)
            bp = xmatch.get("phot_bp_mean_mag", None)
            rp = xmatch.get("phot_rp_mean_mag", None)
            p = xmatch.get("parallax", None)

            x, y = np.nan, np.nan
            if g and bp and rp and p:
                x = bp - rp
                y = g + 5 * np.log10(p / 1000) + 5
            if not math.isnan(x) and not math.isnan(y):
                try:
                    img = plt.imread(current_dir + "/static/img/hr_plot.png")
                    buff = io.BytesIO()

                    fig = plt.figure(figsize=(4, 4), dpi=200)
                    ax = fig.add_subplot(111)
                    ax.plot(
                        x,
                        y,
                        "o",
                        markersize=8,
                        c="#f22f29",
                    )
                    ax.imshow(img, extent=[-1, 5, 17, -5])
                    ax.set_aspect(1 / 4)
                    ax.set_ylabel("G")
                    ax.set_xlabel("BP-RP")
                    ax.set_title(
                        f"BP-RP: {x:.4f}, G: {y:.4f}",
                        fontsize=10,
                        horizontalalignment="center",
                        color="black",
                    )
                    plt.tight_layout(pad=0, h_pad=0, w_pad=0)
                    plt.savefig(buff, dpi=200, bbox_inches="tight")
                    buff.seek(0)
                    plt.close("all")
                    return web.Response(body=buff, content_type="image/png")
                except Exception as e:
                    print(e)

    img = plt.imread(current_dir + "/static/img/hr_plot.png")
    buff = io.BytesIO()
    fig = plt.figure(figsize=(4, 4), dpi=200)
    ax = fig.add_subplot(111)
    ax.imshow(img, extent=[-1, 5, 17, -5])
    ax.set_aspect(1 / 4)
    ax.set_ylabel("G")
    ax.set_xlabel("BP-RP")
    ax.set_title(
        f'No Match in {", ".join(catalogs)}\n (with all required properties)',
        fontsize=16,
        horizontalalignment="center",
        color="red",
    )
    plt.tight_layout(pad=0, h_pad=0, w_pad=0)
    plt.savefig(buff, dpi=200, bbox_inches="tight")
    buff.seek(0)
    plt.close("all")
    return web.Response(body=buff, content_type="image/png")


@routes.get("/sources/{source_id}/images/lc")
@login_required
async def source_lc_get_handler(request):
    """
        Serve light curve plot for a source
    :param request:
    :return:
    """
    # get session:
    await get_session(request)

    _id = request.match_info["source_id"]

    source = (
        await request.app["mongo"]
        .sources.find({"_id": _id}, {"lc": 1})
        .to_list(length=None)
    )
    source = loads(dumps(source[0]))
    # print(source)

    # GET params:
    _r = request.rel_url.query

    # aspect:
    w = float(_r.get("w", 10))
    h = float(_r.get("h", 4))

    # plot mag hist on the right-hand side?
    hist = True if "hist" in _r else False
    # bins
    bins = _r.get("bins", "auto")
    if bins != "auto":
        bins = int(bins)

    # phase-fold?
    period = _r.get("p", None)
    units = str(_r.get("u", "days")).lower()
    if period is not None:
        period = float(period)
        if units == "minutes":
            period /= 24 * 60
        elif units == "hours":
            period /= 24
    plot_twice = _r.get("t", False)

    if len(source["lc"]) > 0:
        try:
            buff = io.BytesIO()

            fig = plt.figure(figsize=(w, h), dpi=200)

            if not hist:
                ax_plc = fig.add_subplot(111)
            else:
                # definitions for the axes
                left, width = 0.1, 0.65
                bottom, height = 0.1, 0.65
                spacing = 0.005

                rect_scatter = [left, bottom, width, height]
                # rect_histx = [left, bottom + height + spacing, width, 0.2]
                rect_histy = [left + width + spacing, bottom, 0.2, height]

                ax_plc = plt.axes(rect_scatter)
                ax_plc.tick_params(direction="in", top=True, right=True)
                ax_histy = plt.axes(rect_histy)
                ax_histy.tick_params(direction="in", labelleft=False)

            if period is None:
                ax_plc.title.set_text(f'Photometric light curve for {source["_id"]}')
            else:
                # r"$\bf{"
                ax_plc.title.set_text(
                    f'Phase-folded light curve for {source["_id"]} with '
                    r"$\bf{"
                    f"p={period}\:{units}"  # noqa W605
                    "}$"
                )

            lc_color_indexes = dict()

            for lc in source["lc"]:
                filt = lc["filter"]
                lc_color_indexes[filt] = (
                    lc_color_indexes[filt] + 1 if filt in lc_color_indexes else 0
                )
                c = lc_colors(filt, lc_color_indexes[filt])

                df_plc = pd.DataFrame.from_records(lc["data"])
                # display(df_plc)

                if "mjd" not in df_plc:
                    df_plc["mjd"] = df_plc["hjd"] - 2400000.5
                if "hjd" not in df_plc:
                    df_plc["hjd"] = df_plc["mjd"] + 2400000.5

                if period is None:
                    # filter out unreleased MSIP data or only use it for QA
                    # w_msip = (df_plc['programid'] != 1) | (df_plc['hjd'] <= t_cut_msip)
                    # w_msip = (df_plc['programid'] == 1) & (df_plc['hjd'] <= t_cut_msip)

                    # w_good = w_msip & (df_plc['catflags'] == 0)
                    if "catflags" in df_plc:
                        w_good = df_plc["catflags"] == 0
                        if np.sum(w_good) > 0:
                            t = df_plc.loc[w_good, "hjd"]
                            mag = df_plc.loc[w_good, "mag"]
                            mag_error = df_plc.loc[w_good, "magerr"]

                            ax_plc.errorbar(
                                t,
                                mag,
                                yerr=mag_error,
                                elinewidth=0.4,
                                marker=".",
                                c=c,
                                lw=0,
                                label=f"filter: {filt}",
                            )

                        # w_not_so_good = w_msip & (df_plc['catflags'] != 0)
                        w_not_so_good = df_plc["catflags"] != 0
                        if np.sum(w_not_so_good) > 0:
                            t = df_plc.loc[w_not_so_good, "hjd"]
                            mag = df_plc.loc[w_not_so_good, "mag"]
                            mag_error = df_plc.loc[w_not_so_good, "magerr"]

                            ax_plc.errorbar(
                                t,
                                mag,
                                yerr=mag_error,
                                elinewidth=0.4,
                                marker="x",
                                alpha=0.5,
                                c=c,
                                lw=0,
                                label=f"filter: {filt}, flagged",
                            )
                    else:
                        w_det = df_plc["mag"] != 0
                        t = df_plc.loc[w_det, "hjd"]
                        mag = df_plc.loc[w_det, "mag"]
                        mag_error = df_plc.loc[w_det, "magerr"]

                        ax_plc.errorbar(
                            t,
                            mag,
                            yerr=mag_error,
                            elinewidth=0.4,
                            marker=".",
                            c=c,
                            lw=0,
                            label=f"filter: {filt}",
                        )

                else:
                    # phase-folded lc:
                    if "catflags" in df_plc:
                        w_det = (df_plc["mag"] != 0) & (df_plc["catflags"] == 0)
                    else:
                        w_det = df_plc["mag"] != 0

                    df_plc["phase"] = df_plc["hjd"].apply(lambda x: (x / period) % 1)

                    t = (
                        df_plc.loc[w_det, "phase"]
                        if not plot_twice
                        else np.hstack(
                            (
                                df_plc.loc[w_det, "phase"].values,
                                df_plc.loc[w_det, "phase"].values + 1,
                            )
                        )
                    )
                    mag = (
                        df_plc.loc[w_det, "mag"]
                        if not plot_twice
                        else np.hstack(
                            (
                                df_plc.loc[w_det, "mag"].values,
                                df_plc.loc[w_det, "mag"].values,
                            )
                        )
                    )
                    mag_error = (
                        df_plc.loc[w_det, "magerr"].values
                        if not plot_twice
                        else np.hstack(
                            (
                                df_plc.loc[w_det, "magerr"].values,
                                df_plc.loc[w_det, "magerr"].values,
                            )
                        )
                    )

                    ax_plc.errorbar(
                        t,
                        mag,
                        yerr=mag_error,
                        elinewidth=0.4,
                        marker=".",
                        c=c,
                        lw=0,
                        label=f"filter: {filt}",
                    )

                if hist:
                    if period is not None:
                        if "catflags" in df_plc:
                            w_det = (df_plc["mag"] != 0) & (df_plc["catflags"] == 0)
                        else:
                            w_det = df_plc["mag"] != 0
                    else:
                        w_det = df_plc["mag"] != 0
                    mag = df_plc.loc[w_det, "mag"]
                    ax_histy.hist(
                        mag,
                        bins=bins,
                        color=c,
                        alpha=0.5,
                        label=f"filter: {filt}",
                        orientation="horizontal",
                    )

            ax_plc.invert_yaxis()
            # if t_format == 'days_ago':
            #     ax_plc.invert_xaxis()
            ax_plc.grid(True, lw=0.3)
            # ax_plc.set_xlabel(t_format)
            ax_plc.set_ylabel("mag")

            if not hist:
                ax_plc.legend(
                    bbox_to_anchor=(1, 1), loc="upper left", ncol=1, fontsize="x-small"
                )
                plt.tight_layout(pad=0, h_pad=0, w_pad=0)
            else:
                ax_histy.invert_yaxis()
                ax_histy.grid(True, lw=0.3)
                ax_histy.legend(
                    bbox_to_anchor=(1, 1), loc="upper left", ncol=1, fontsize="x-small"
                )

            plt.savefig(buff, dpi=200, bbox_inches="tight")
            buff.seek(0)
            plt.close("all")
            return web.Response(body=buff, content_type="image/png")
        except Exception as e:
            print(e)

    buff = io.BytesIO()
    # buff.write(base64.b64decode(b"R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"))
    # buff.seek(0)
    # return web.Response(body=buff, content_type='image/gif')
    return web.Response(body=buff, content_type="image/png")


@routes.get("/sources/{source_id}/images/maghist")
@login_required
async def source_maghist_get_handler(request):
    """
        Serve mag hist for a source
    :param request:
    :return:
    """
    # get session:
    await get_session(request)

    _id = request.match_info["source_id"]

    source = (
        await request.app["mongo"]
        .sources.find({"_id": _id}, {"lc": 1})
        .to_list(length=None)
    )
    source = loads(dumps(source[0]))
    # print(source)

    # GET params:
    _r = request.rel_url.query

    # aspect:
    w = float(_r.get("w", 4.3))
    h = float(_r.get("h", 4))
    # bins
    bins = _r.get("bins", "auto")
    if bins != "auto":
        bins = int(bins)

    if len(source["lc"]) > 0:
        try:
            buff = io.BytesIO()

            fig = plt.figure(figsize=(w, h), dpi=200)
            ax_plc = fig.add_subplot(111)
            ax_plc.title.set_text(f'Histogram of magnitudes for {source["_id"]}')

            lc_color_indexes = dict()

            for lc in source["lc"]:
                filt = lc["filter"]
                lc_color_indexes[filt] = (
                    lc_color_indexes[filt] + 1 if filt in lc_color_indexes else 0
                )
                c = lc_colors(filt, lc_color_indexes[filt])

                df_plc = pd.DataFrame.from_records(lc["data"])
                # display(df_plc)

                if "mjd" not in df_plc:
                    df_plc["mjd"] = df_plc["hjd"] - 2400000.5
                if "hjd" not in df_plc:
                    df_plc["hjd"] = df_plc["mjd"] + 2400000.5

                if "catflags" in df_plc:
                    w_det = (df_plc["mag"] != 0) & (df_plc["catflags"] == 0)
                else:
                    w_det = df_plc["mag"] != 0

                mag = df_plc.loc[w_det, "mag"]

                ax_plc.hist(mag, bins=bins, color=c, alpha=0.5, label=f"filter: {filt}")

            ax_plc.grid(True, lw=0.3)
            ax_plc.set_xlabel("mag")
            ax_plc.legend(loc="best", ncol=1, fontsize="x-small")

            plt.tight_layout(pad=0, h_pad=0, w_pad=0)

            plt.savefig(buff, dpi=200, bbox_inches="tight")
            buff.seek(0)
            plt.close("all")
            return web.Response(body=buff, content_type="image/png")
        except Exception as e:
            print(e)

    buff = io.BytesIO()
    return web.Response(body=buff, content_type="image/png")


def cross_match(kowalski, ra, dec):
    kowalski_query_xmatch = {
        "query_type": "cone_search",
        "query": {
            "object_coordinates": {
                "radec": f"[({ra}, {dec})]",
                "cone_search_radius": config["kowalski"]["cross_match"][
                    "cone_search_radius"
                ],
                "cone_search_unit": config["kowalski"]["cross_match"][
                    "cone_search_unit"
                ],
            },
            "catalogs": config["kowalski"]["cross_match"]["catalogs"],
        },
    }
    # print(kowalski_query_xmatch)

    resp = kowalski.query(query=kowalski_query_xmatch, name="kowalski")
    xmatch = {}
    for instance_result in resp.values():
        xmatch = {**xmatch, **instance_result["data"]}

    # reformat for ingestion (we queried only one sky position):
    for cat in xmatch.keys():
        kk = list(xmatch[cat].keys())[0]
        xmatch[cat] = xmatch[cat][kk]

    return xmatch


@routes.put("/sources")
@login_required
async def sources_put_handler(request):
    """
        Save ZTF source to own db assigning a unique id and adding to a program,
        or create a blank one
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)
    user = session["user_id"]

    try:
        _r = await request.json()
    except Exception as _e:
        print(f"Cannot extract json() from request, trying post(): {str(_e)}")
        _r = await request.post()
    # print(_r)

    try:
        # assert ('_id' in _r) or (('ra' in _r) and ('dec' in _r)), '_id or (ra, dec) not specified'
        # assert 'zvm_program_id' in _r, 'zvm_program_id not specified'
        # assert 'catalog' in _r, 'catalog not specified'

        _id = _r.get("_id", None)
        ra = _r.get("ra", None)
        dec = _r.get("dec", None)
        catalog = _r.get("catalog", None)
        zvm_program_id = _r.get("zvm_program_id", None)
        automerge = _r.get("automerge", False)
        return_result = _r.get("return_result", True)
        prefix = _r.get("prefix", "ZTFS")
        naming = _r.get("naming", "incremental")  # 'incremental' or 'random'

        assert zvm_program_id is not None, "zvm_program_id not specified"
        assert (_id is not None) or (
            (ra is not None) and (dec is not None)
        ), "_id or (ra, dec) not specified"
        assert catalog is not None, "catalog not specified"

        instance = None
        catalogs = []
        for inst_name, inst in request.app["kowalski"].instances.items():
            catalogs.extend(inst.get("catalogs", []))
            if catalog in inst.get("catalogs", []):
                instance = inst_name
        if instance is None:
            return web.json_response(
                {"message": f"failure: no instance found for catalog {catalog}"},
                status=400,
            )
        catalogs = [c for c in catalogs if c.startswith("ZTF_sources_202")]

        # print(_r)

        if _id is not None:
            kowalski_query = {
                "query_type": "find",
                "query": {
                    "catalog": catalog,
                    "filter": {"_id": int(_r["_id"])},
                    "projection": {
                        "_id": 1,
                        "ra": 1,
                        "dec": 1,
                        "filter": 1,
                        "coordinates": 1,
                        "data": 1,
                    },
                },
            }

            resp = request.app["kowalski"].query(kowalski_query)
            ztf_sources = resp.get(instance, {}).get("data", [])
            assert len(ztf_sources) > 0, f"failure: {_id} not found in {catalog}"
            ztf_source = ztf_sources[0]

        else:
            ztf_source = parse_radec(ra, dec)

        # build doc to ingest:
        doc = dict()

        if naming == "incremental":
            source_id_base = f'{prefix}{datetime.datetime.utcnow().strftime("%y")}{ztf_source["coordinates"]["radec_str"][0][:2]}'

            num_saved_sources = await request.app["mongo"].sources.count_documents(
                {"_id": {"$regex": f"{source_id_base}.*"}}
            )
            # postfix = num2alphabet(num_saved_sources + 1)
            if num_saved_sources > 0:
                saved_source_ids = (
                    await request.app["mongo"]
                    .sources.find(
                        {"_id": {"$regex": f"{source_id_base}.*"}}, {"_id": 1}
                    )
                    .to_list(length=None)
                )
                saved_source_ids = [s["_id"] for s in saved_source_ids]
                saved_source_ids.sort(key=lambda item: (len(item), item))
                # print(saved_source_ids)
                num_last = alphabet2num(saved_source_ids[-1][8:])

                postfix = num2alphabet(num_last + 1)

            else:
                postfix = "a"

            source_id = source_id_base + postfix
        else:
            # random naming
            source_id = uid(prefix=prefix, length=8)

        # unique (sequential) id:
        doc["_id"] = source_id

        # assign to zvm_program_id:
        doc["zvm_program_id"] = int(_r["zvm_program_id"])

        # coordinates:
        doc["ra"] = ztf_source["ra"]
        doc["dec"] = ztf_source["dec"]
        # Galactic coordinates:
        doc["l"], doc["b"] = radec2lb(doc["ra"], doc["dec"])  # longitude, latitude
        doc["coordinates"] = ztf_source["coordinates"]

        # [{'period': float, 'period_error': float}]:
        doc["p"] = []
        doc["source_types"] = []
        doc["source_flags"] = []
        doc["history"] = []

        doc["labels"] = []

        # cross match:
        xmatch = cross_match(
            kowalski=request.app["kowalski"], ra=doc["ra"], dec=doc["dec"]
        )
        doc["xmatch"] = xmatch

        # spectra
        doc["spec"] = []

        # lc:
        if "data" in ztf_source:
            # filter lc for MSIP data
            if config["misc"]["filter_MSIP"]:
                # print(len(ztf_source['data']))
                # ztf_source['data'] = [dp for dp in ztf_source['data'] if dp['programid'] != 1]
                ztf_source["data"] = [
                    dp
                    for dp in ztf_source["data"]
                    if (
                        (dp["programid"] != 1)
                        or (
                            dp["hjd"] - 2400000.5
                            <= config["misc"]["filter_MSIP_best_before_mjd"]
                        )
                    )
                ]
                # print(len(ztf_source['data']))

            # temporal, folded; if folded - 'p': [{'period': float, 'period_error': float}]
            lc = {
                "_id": uid(length=24),
                "telescope": "PO:1.2m",
                "instrument": "ZTF",
                "release": config["kowalski"]["coll_sources"],
                "id": ztf_source["_id"],
                "filter": ztf_source["filter"],
                "lc_type": "temporal",
                "data": ztf_source["data"],
            }
            doc["lc"] = [lc]
        else:
            doc["lc"] = []

        # feelin' lucky?
        if automerge:
            query_merge = {
                "query_type": "cone_search",
                "query": {
                    "object_coordinates": {
                        "radec": f"[({doc['ra']}, {doc['dec']})]",
                        # "cone_search_radius": config['kowalski']['cross_match']['cone_search_radius'],
                        # "cone_search_unit": config['kowalski']['cross_match']['cone_search_unit']},
                        "cone_search_radius": "2",
                        "cone_search_unit": "arcsec",
                    },
                    "catalogs": {
                        catalog: {
                            "filter": {},
                            "projection": {
                                "_id": 1,
                                "ra": 1,
                                "dec": 1,
                                "filter": 1,
                                "coordinates": 1,
                                "data": 1,
                            }
                            # "projection": {'_id': 1, 'ra': 1, 'dec': 1, 'filter': 1, 'coordinates': 1}
                        }
                    },
                },
            }
            if _id is not None:
                # skip the one that is already there:
                query_merge["query"]["catalogs"][catalog]["filter"] = {
                    "_id": {"$ne": int(_id)}
                }
            # print(query_merge)

            resp = request.app["kowalski"].query(query_merge)
            kk = list(resp.get(instance, {}).get("data", {}).get(catalog, {}).keys())[0]
            sources_merge = resp["data"][catalog][kk]
            # print(sources_merge)

            for source_merge in sources_merge:
                # filter lc for MSIP data
                if config["misc"]["filter_MSIP"]:
                    source_merge["data"] = [
                        dp
                        for dp in source_merge["data"]
                        if (
                            (dp["programid"] != 1)
                            or (
                                dp["hjd"] - 2400000.5
                                <= config["misc"]["filter_MSIP_best_before_mjd"]
                            )
                        )
                    ]

                # temporal, folded; if folded - 'p': [{'period': float, 'period_error': float}]
                lc = {
                    "_id": uid(length=24),
                    "telescope": "PO:1.2m",
                    "instrument": "ZTF",
                    "release": catalog,
                    "id": source_merge["_id"],
                    "filter": source_merge["filter"],
                    "lc_type": "temporal",
                    "data": source_merge["data"],
                }

                doc["lc"].append(lc)

        doc["created_by"] = user
        time_tag = utc_now()
        doc["created"] = time_tag
        doc["last_modified"] = time_tag

        # make history
        doc["history"].append(
            {"note_type": "info", "time_tag": time_tag, "user": user, "note": "Saved"}
        )

        if naming == "incremental":
            await request.app["mongo"].sources.insert_one(doc)
        else:
            # avoid random name collisions
            for nr in range(config["misc"]["max_retries"]):
                try:
                    doc["_id"] = uid(prefix=prefix, length=8)
                    await request.app["mongo"].sources.insert_one(doc)
                    break
                except pymongo.errors.DuplicateKeyError:
                    continue

        if return_result:
            return web.json_response(
                {"message": "success", "result": doc}, status=200, dumps=dumps
            )
        else:
            return web.json_response(
                {"message": "success", "result": {"_id": doc["_id"]}},
                status=200,
                dumps=dumps,
            )

    except Exception as _e:
        print(f"Failed to ingest source: {str(_e)}")
        _err = traceback.format_exc()
        print(str(_err))

        try:
            if not request.app["kowalski"].ping():
                print("Apparently lost connection to Kowalski, trying to reset")
                request.app["kowalski"] = Kowalski(
                    instances=config["kowalski"]["instances"]
                )
                print("Success")
        except Exception as __e:
            print(str(__e))

        return web.json_response({"message": f"ingestion failed {str(_e)}"}, status=200)


class MyMultipartReader(multipart.MultipartReader):
    def _get_boundary(self):
        return super()._get_boundary()  # + '\r\n'


@routes.post("/sources/{source_id}")
@login_required
async def source_post_handler(request):
    """
        Update saved source
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)
    user = session["user_id"]

    try:
        _r = await request.json()
    except Exception as _e:
        print(f"Cannot extract json() from request, trying post(): {str(_e)}")
        try:
            _r = await request.post()
        except Exception as _ee:
            print(f"Cannot extract post() from request, trying multipart(): {str(_ee)}")
            print(await request.text())
            _r = MyMultipartReader(request._headers, request._payload)
            _r = await _r.next()
    # print(_r)

    catalog = _r.get("catalog", None)

    try:
        _id = request.match_info["source_id"]

        source = await request.app["mongo"].sources.find_one(
            {"_id": _id}, {"lc.data": 0}
        )

        if "action" in _r:

            if _r["action"] == "merge":
                if catalog is None:
                    return web.json_response(
                        {"message": "failure: catalog not specified"}, status=400
                    )

                instance = None
                catalogs = []
                for inst_name, inst in request.app["kowalski"].instances.items():
                    catalogs.extend(inst.get("catalogs", []))
                    if catalog in inst.get("catalogs", []):
                        instance = inst_name
                if instance is None:
                    return web.json_response(
                        {
                            "message": f"failure: no instance found for catalog {catalog}"
                        },
                        status=400,
                    )
                catalogs = [c for c in catalogs if c.startswith("ZTF_sources_202")]
                # merge a ZTF light curve with saved source

                ztf_lc_ids = [
                    llc["id"] for llc in source["lc"] if llc["instrument"] == "ZTF"
                ]
                # print(ztf_lc_ids)

                if "_id" not in _r:
                    return web.json_response(
                        {"message": "failure: _id not specified"}, status=500
                    )

                # lc already there? then replace!
                if int(_r["_id"]) in ztf_lc_ids:
                    # print(_r['_id'], ztf_lc_ids, _r['_id'] in ztf_lc_ids)
                    # first pull it from lc array:
                    await request.app["mongo"].sources.update_one(
                        {"lc.id": int(_r["_id"])},
                        {"$pull": {"lc": {"id": int(_r["_id"])}}},
                    )

                kowalski_query = {
                    "query_type": "find",
                    "query": {
                        "catalog": catalog,
                        "filter": {"_id": int(_r["_id"])},
                        "projection": {
                            "_id": 1,
                            "ra": 1,
                            "dec": 1,
                            "filter": 1,
                            "coordinates": 1,
                            "data": 1,
                        },
                    },
                }

                resp = request.app["kowalski"].query(kowalski_query)
                ztf_sources = resp.get(instance, {}).get("data", [])
                if len(ztf_sources) == 0:
                    return web.json_response(
                        {"message": "failure: no such source in selected catalog"},
                        status=500,
                    )
                ztf_source = ztf_sources[0]

                # filter lc for MSIP data
                if config["misc"]["filter_MSIP"]:
                    # print(len(ztf_source['data']))
                    # ztf_source['data'] = [dp for dp in ztf_source['data'] if dp['programid'] != 1]
                    ztf_source["data"] = [
                        dp
                        for dp in ztf_source["data"]
                        if (
                            (dp["programid"] != 1)
                            or (
                                dp["hjd"] - 2400000.5
                                <= config["misc"]["filter_MSIP_best_before_mjd"]
                            )
                        )
                    ]
                    # print(len(ztf_source['data']))

                lc = {
                    "_id": random_alphanumeric_str(length=24),
                    "telescope": "PO:1.2m",
                    "instrument": "ZTF",
                    "release": config["kowalski"]["coll_sources"],
                    "id": ztf_source["_id"],
                    "filter": ztf_source["filter"],
                    "lc_type": "temporal",
                    "data": ztf_source["data"],
                }

                # make history
                time_tag = utc_now()
                h = {
                    "note_type": "merge",
                    "time_tag": time_tag,
                    "user": user,
                    "note": f'{ztf_source["_id"]}',
                }

                await request.app["mongo"].sources.update_one(
                    {"_id": _id},
                    {
                        "$push": {"lc": lc, "history": h},
                        "$set": {"last_modified": utc_now()},
                    },
                )

                return web.json_response({"message": "success"}, status=200)

            elif _r["action"] == "upload_lc":
                # upload light curve

                lcs = _r["data"]

                if isinstance(lcs, dict):
                    lcs = [lcs]

                for lc in lcs:
                    # generate unique _id:
                    lc["_id"] = random_alphanumeric_str(length=24)
                    # check data format:
                    for kk in (
                        "telescope",
                        "instrument",
                        "filter",
                        "id",
                        "lc_type",
                        "data",
                    ):
                        assert kk in lc, f"{kk} key not set"
                    for idp, dp in enumerate(lc["data"]):
                        # fixme when the time comes:
                        is_goed = (("mag" in dp) and ("magerr" in dp)) or (
                            ("mag_llim" in dp) or ("mag_ulim" in dp)
                        )
                        assert is_goed, f"bad photometry for data point #{idp+1}"
                        assert ("mjd" in dp) or (
                            "hjd" in dp
                        ), f"time stamp (mjd/hjd) not set for data point #{idp + 1}"
                        # some people pathologically like strings:
                        for kk in (
                            "mag",
                            "magerr",
                            "mag_llim",
                            "mag_ulim",
                            "mjd",
                            "hjd",
                        ):
                            if (kk in dp) and (not isinstance(dp[kk], float)):
                                dp[kk] = float(dp[kk])

                    # make history
                    time_tag = utc_now()
                    h = {
                        "note_type": "lc",
                        "time_tag": time_tag,
                        "user": user,
                        "note": f'{lc["telescope"]} {lc["instrument"]} {lc["filter"]} {lc["id"]}',
                    }

                    await request.app["mongo"].sources.update_one(
                        {"_id": _id},
                        {
                            "$push": {"lc": lc, "history": h},
                            "$set": {"last_modified": utc_now()},
                        },
                    )

                return web.json_response({"message": "success"}, status=200)

            elif _r["action"] == "remove_lc":
                # upload light curve

                lc_id = _r["lc_id"]

                # make history
                time_tag = utc_now()
                h = {
                    "note_type": "lc",
                    "time_tag": time_tag,
                    "user": user,
                    "note": f"removed {lc_id}",
                }

                await request.app["mongo"].sources.update_one(
                    {"_id": _id},
                    {
                        "$pull": {"lc": {"_id": lc_id}},
                        "$push": {"history": h},
                        "$set": {"last_modified": utc_now()},
                    },
                )

                return web.json_response({"message": "success"}, status=200)

            elif _r["action"] == "upload_spectrum":
                # upload spectrum

                spectrum = _r["data"]

                # generate unique _id:
                spectrum["_id"] = random_alphanumeric_str(length=24)

                # check data format:
                for kk in (
                    "telescope",
                    "instrument",
                    "filter",
                    "wavelength_unit",
                    "flux_unit",
                    "data",
                ):
                    assert kk in spectrum, f"{kk} key not set"
                assert ("mjd" in spectrum) or (
                    "hjd" in spectrum
                ), "time stamp (mjd/hjd) not set"

                for idp, dp in enumerate(spectrum["data"]):
                    # fixme when the time comes:
                    for kk in ("wavelength", "flux", "fluxerr"):
                        assert kk in dp, f"{kk} key not set for data point #{idp + 1}"

                # make history
                time_tag = utc_now()
                h = {
                    "note_type": "spec",
                    "time_tag": time_tag,
                    "user": user,
                    "note": f'{spectrum["telescope"]} {spectrum["instrument"]} {spectrum["filter"]}',
                }

                await request.app["mongo"].sources.update_one(
                    {"_id": _id},
                    {
                        "$push": {"spec": spectrum, "history": h},
                        "$set": {"last_modified": utc_now()},
                    },
                )

                return web.json_response({"message": "success"}, status=200)
                # return web.json_response({'message': 'failure: not implemented'}, status=200)

            elif _r["action"] == "remove_spectrum":
                # remove spectrum

                spectrum_id = _r["spectrum_id"]

                # make history
                time_tag = utc_now()
                h = {
                    "note_type": "spec",
                    "time_tag": time_tag,
                    "user": user,
                    "note": f"removed {spectrum_id}",
                }

                await request.app["mongo"].sources.update_one(
                    {"_id": _id},
                    {
                        "$pull": {"spec": {"_id": spectrum_id}},
                        "$push": {"history": h},
                        "$set": {"last_modified": utc_now()},
                    },
                )

                return web.json_response({"message": "success"}, status=200)

            elif _r["action"] == "transfer_source":
                # add note
                new_pid = _r["zvm_program_id"]

                # make history
                time_tag = utc_now()
                h = {
                    "note_type": "transfer",
                    "time_tag": time_tag,
                    "user": user,
                    "note": new_pid,
                }

                await request.app["mongo"].sources.update_one(
                    {"_id": _id},
                    {
                        "$push": {"history": h},
                        "$set": {
                            "zvm_program_id": int(new_pid),
                            "last_modified": time_tag,
                        },
                    },
                )

                return web.json_response({"message": "success"}, status=200)

            elif _r["action"] == "add_note":
                # add note
                note = _r["note"]

                # make history
                time_tag = utc_now()
                h = {
                    "note_type": "note",
                    "time_tag": time_tag,
                    "user": user,
                    "note": note,
                }

                await request.app["mongo"].sources.update_one(
                    {"_id": _id},
                    {"$push": {"history": h}, "$set": {"last_modified": time_tag}},
                )

                return web.json_response({"message": "success"}, status=200)

            elif _r["action"] == "add_source_type":
                # add source type
                source_type = _r["source_type"]

                if source_type in source["source_types"]:
                    return web.json_response(
                        {"message": "source type already added"}, status=200
                    )

                # make history
                time_tag = utc_now()
                h = {
                    "note_type": "type",
                    "time_tag": time_tag,
                    "user": user,
                    "note": source_type,
                }

                await request.app["mongo"].sources.update_one(
                    {"_id": _id},
                    {
                        "$push": {"source_types": source_type, "history": h},
                        "$set": {"last_modified": time_tag},
                    },
                )

                return web.json_response({"message": "success"}, status=200)

            elif _r["action"] == "add_period":
                # add period
                period = _r["period"]
                period_unit = _r["period_unit"].capitalize()

                known_units = ["Minutes", "Hours", "Days"]
                assert (
                    period_unit in known_units
                ), f"period unit {period_unit} not in {known_units}"

                p = {"period": period, "period_unit": period_unit}

                if p in source["p"]:
                    return web.json_response(
                        {"message": "period already added"}, status=200
                    )

                # make history
                time_tag = utc_now()
                h = {
                    "note_type": "period",
                    "time_tag": time_tag,
                    "user": user,
                    "note": f"{period} {period_unit}",
                }

                await request.app["mongo"].sources.update_one(
                    {"_id": _id},
                    {
                        "$push": {"p": p, "history": h},
                        "$set": {"last_modified": time_tag},
                    },
                )

                return web.json_response({"message": "success"}, status=200)

            elif _r["action"] == "add_source_flags":
                # add source flags
                source_flags = _r["source_flags"]

                # todo: check flags?

                # make history
                time_tag = utc_now()
                h = {
                    "note_type": "flag",
                    "time_tag": time_tag,
                    "user": user,
                    "note": source_flags,
                }

                await request.app["mongo"].sources.update_one(
                    {"_id": _id},
                    {
                        "$push": {"history": h},
                        "$set": {
                            "source_flags": source_flags,
                            "last_modified": time_tag,
                        },
                    },
                )

                return web.json_response({"message": "success"}, status=200)

            elif _r["action"] == "run_cross_match":

                xmatch = cross_match(
                    kowalski=request.app["kowalski"], ra=source["ra"], dec=source["dec"]
                )

                # make history
                time_tag = utc_now()
                h = {
                    "note_type": "info",
                    "time_tag": time_tag,
                    "user": user,
                    "note": "Cross-matched",
                }

                await request.app["mongo"].sources.update_one(
                    {"_id": _id},
                    {
                        "$push": {"history": h},
                        "$set": {"xmatch": xmatch, "last_modified": time_tag},
                    },
                )

                return web.json_response({"message": "success"}, status=200)

            elif _r["action"] == "set_labels":
                # set labels
                labels = _r.get("labels", [])

                # make history. don't! too much info, will flood history, esp. w autosave on
                time_tag = utc_now()
                # h = {'note_type': 'labels',
                #      'time_tag': time_tag,
                #      'user': user,
                #      'note': labels}

                # spice up
                for label in labels:
                    label["user"] = user
                    label["last_modified"] = time_tag

                doc = (
                    await request.app["mongo"]
                    .sources.find({"_id": _id}, {"_id": 0, "labels": 1})
                    .to_list(length=None)
                )
                # ditch user's old labels:
                labels_current = [
                    lab
                    for lab in doc[0].get("labels", ())
                    if lab.get("user", None) != user
                ]
                # print(labels_current)

                await request.app["mongo"].sources.update_one(
                    {"_id": _id},
                    {  # '$push': {'history': h},
                        "$set": {
                            "labels": labels + labels_current,
                            "last_modified": time_tag,
                        }
                    },
                )

                return web.json_response({"message": "success"}, status=200)

            else:
                return web.json_response(
                    {"message": "failure: unknown action requested"}, status=200
                )

        else:
            return web.json_response(
                {"message": "failure: action not specified"}, status=200
            )

    except Exception as _e:
        print(f"POST failed: {str(_e)}")
        _err = traceback.format_exc()
        print(_err)
        return web.json_response({"message": f"action failed: {str(_e)}"}, status=200)


@routes.delete("/sources/{source_id}")
@login_required
async def source_delete_handler(request):
    """
        Update saved source
    :param request:
    :return:
    """
    # get session:
    await get_session(request)

    # try:
    #     _r = await request.json()
    # except Exception as _e:
    #     # print(f'Cannot extract json() from request, trying post(): {str(_e)}')
    #     _r = await request.post()
    # # print(_r)

    try:
        _id = request.match_info["source_id"]

        await request.app["mongo"].sources.delete_one({"_id": _id})

        # todo: delete associated data (e.g. finding chart)

        return web.json_response({"message": "success"}, status=200)

    except Exception as _e:
        print(f"Failed to merge source: {str(_e)}")

        return web.json_response({"message": f"deletion failed: {str(_e)}"}, status=200)


""" search ZTF light curve db """


@routes.get("/search")
@login_required
async def search_get_handler(request):
    """
        Serve GS page for the browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    # get ZVM programs:
    programs = (
        await request.app["mongo"]
        .programs.find({}, {"last_modified": 0})
        .to_list(length=None)
    )

    catalogs = []
    for instance in request.app["kowalski"].instances.values():
        catalogs.extend(instance.get("catalogs", []))

    # keep only those that start with ZTF_sources_202
    catalogs = sorted(
        [c for c in catalogs if c.startswith("ZTF_sources_202")], reverse=True
    )

    # fixme: redo catalogs once PTF light curves are ingested
    context = {
        "logo": config["server"]["logo"],
        "user": session["user_id"],
        "programs": programs,
        "catalogs": catalogs,
    }
    response = aiohttp_jinja2.render_template("template-search.html", request, context)
    return response


@routes.post("/search")
@login_required
async def search_post_handler(request):
    """
        Process Kowalski query
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    try:
        _query = await request.json()
    except Exception as _e:
        print(f"Cannot extract json() from request, trying post(): {str(_e)}")
        # _err = traceback.format_exc()
        # print(_err)
        _query = await request.post()

    catalogs = []
    try:
        for instance in request.app["kowalski"].instances.values():
            catalogs.extend(instance.get("catalogs", []))

        # keep only those that start with ZTF_sources_202
        catalogs = sorted(
            [c for c in catalogs if c.startswith("ZTF_sources_202")], reverse=True
        )
    except Exception as _e:
        print(f"Failed to get catalogs: {str(_e)}")

    error = None
    try:
        if _query is None or not isinstance(_query, MultiDictProxy) or len(_query) == 0:
            error = "failure: no query specified"
        elif (
            "radec" not in _query
            or not isinstance(_query["radec"], str)
            or len(_query["radec"]) == 0
        ):
            error = "failure: no radec specified"
        elif (
            "cone_search_radius" not in _query
            or not isinstance(_query["cone_search_radius"], (int, float, str))
            or len(_query["cone_search_radius"]) == 0
        ):
            error = "failure: no cone_search_radius specified"
        elif (
            not str(_query["cone_search_radius"]).isnumeric()
            or float(_query["cone_search_radius"]) <= 0
        ):
            error = "failure: cone_search_radius must be a positive number and not zero"
        elif (
            "cone_search_unit" not in _query
            or not isinstance(_query["cone_search_unit"], str)
            or _query["cone_search_unit"] not in ("arcsec", "arcmin", "deg")
        ):
            error = "failure: no cone_search_unit specified"
        elif (
            "catalogs" not in _query
            or not isinstance(_query["catalogs"], str)
            or len(_query["catalogs"]) == 0
        ):
            error = "failure: no catalogs specified"
        elif _query["catalogs"] not in catalogs:
            error = "failure: invalid catalogs specified, must be in " + ", ".join(
                catalogs
            )
    except Exception as _e:
        error = f"failure: invalid query: {str(_e)}"

    if error is not None:
        context = {
            "logo": config["server"]["logo"],
            "user": session["user_id"],
            "programs": [],
            "messages": [[str(error), "danger"]],
            "catalogs": catalogs,
            "form": _query,
        }
        response = aiohttp_jinja2.render_template(
            "template-search.html", request, context
        )
        return response

    try:
        # convert to Kowalski query and execute

        # comb radecs for single sources as per Tom's request:
        radec = _query["radec"].strip()
        if radec[0] not in ("[", "(", "{"):
            ra, dec = radec.split()
            if ("s" in radec) or (":" in radec):
                radec = f"[('{ra}', '{dec}')]"
            else:
                radec = f"[({ra}, {dec})]"

        catalog = _query.get("catalogs", None)

        kowalski_query = {
            "query_type": "cone_search",
            "query": {
                "object_coordinates": {
                    "radec": radec,
                    "cone_search_radius": _query["cone_search_radius"],
                    "cone_search_unit": _query["cone_search_unit"],
                },
                "catalogs": {
                    catalog: {
                        "filter": _query["filter"]
                        if len(_query.get("filter", {})) > 0
                        else {},
                        "projection": {
                            "_id": 1,
                            "ra": 1,
                            "dec": 1,
                            "magrms": 1,
                            "maxmag": 1,
                            "vonneumannratio": 1,
                            "filter": 1,
                            "maxslope": 1,
                            "meanmag": 1,
                            "medianabsdev": 1,
                            "medianmag": 1,
                            "minmag": 1,
                            "nobs": 1,
                            "refchi": 1,
                            "refmag": 1,
                            "refmagerr": 1,
                            "iqr": 1,
                            "data.mag": 1,
                            "data.magerr": 1,
                            "data.hjd": 1,
                            "data.programid": 1,
                            "coordinates": 1,
                        },
                    }
                },
            },
        }

        resp = request.app["kowalski"].query(kowalski_query)
        # print(resp)
        sources = []
        for instance_results in resp.values():
            for catalog_results in instance_results.get("data", {}).values():
                for cone in catalog_results.values():
                    sources.extend(cone)

        data_formatted = []
        for source in sources:
            # re-format data (mjd, mag, magerr) for easier previews in the browser:

            lc = source["data"]

            # filter lc for MSIP data
            if config["misc"]["filter_MSIP"]:
                lc = [
                    p
                    for p in lc
                    if (
                        (p["programid"] != 1)
                        or (
                            p["hjd"] - 2400000.5
                            <= config["misc"]["filter_MSIP_best_before_mjd"]
                        )
                    )
                ]
                if len(lc) == 0:
                    continue

            # print(lc)
            mags = np.array([llc["mag"] for llc in lc])
            magerrs = np.array([llc["magerr"] for llc in lc])
            hjds = np.array([llc["hjd"] for llc in lc])
            mjds = hjds - 2400000.5
            datetimes = np.array(
                [
                    mjd_to_datetime(llc["hjd"] - 2400000.5).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    for llc in lc
                ]
            )

            ind_sort = np.argsort(mjds)
            mags = mags[ind_sort].tolist()
            magerrs = magerrs[ind_sort].tolist()
            mjds = mjds[ind_sort].tolist()
            datetimes = datetimes[ind_sort].tolist()

            source.pop("data", None)
            source["mag"] = mags
            source["magerr"] = magerrs
            # source['mjd'] = mjds
            source["mjd"] = datetimes
            source["catalog"] = catalog

            data_formatted.append(source)

        # get ZVM programs:
        programs = (
            await request.app["mongo"]
            .programs.find({}, {"last_modified": 0})
            .to_list(length=None)
        )

        context = {
            "logo": config["server"]["logo"],
            "user": session["user_id"],
            "data": data_formatted,
            "programs": programs,
            "catalogs": catalogs,
            "form": _query,
        }
        response = aiohttp_jinja2.render_template(
            "template-search.html", request, context
        )
        return response

    except Exception as _e:
        print(f"Querying Kowalski failed: {str(_e)}")

        try:
            if not request.app["kowalski"].ping():
                print("Apparently lost connection to Kowalski, trying to reset")
                request.app["kowalski"] = Kowalski(
                    instances=config["kowalski"]["instances"]
                )
                print("Success")
        except Exception as __e:
            print(str(__e))

        context = {
            "logo": config["server"]["logo"],
            "user": session["user_id"],
            "programs": [],
            "messages": [[str(_e), "danger"]],
        }
        response = aiohttp_jinja2.render_template(
            "template-search.html", request, context
        )
        return response


""" web endpoints """


@routes.get("/docs")
@login_required
async def docs_handler(request):
    """
        Serve docs page for the browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    # todo?

    context = {"logo": config["server"]["logo"], "user": session["user_id"]}
    response = aiohttp_jinja2.render_template("template-docs.html", request, context)
    return response


@routes.get("/docs/{doc}")
@login_required
async def doc_handler(request):
    """
        Serve doc page for the browser
    :param request:
    :return:
    """
    # get session:
    session = await get_session(request)

    doc = request.match_info["doc"]

    title = doc.replace("_", " ").capitalize()

    # render doc with misaka
    with open(os.path.join(config["path"]["path_docs"], doc + ".md"), "r") as f:
        tut = f.read()

    content = md(tut)

    context = {
        "logo": config["server"]["logo"],
        "user": session["user_id"],
        "title": title,
        "content": content,
    }
    response = aiohttp_jinja2.render_template("template-doc.html", request, context)
    return response


async def app_factory():
    """
        App Factory
    :return:
    """

    # init db if necessary
    await init_db()

    # Database connection
    if config["database"].get("srv", False) is True:
        conn_string = "mongodb+srv://"
    else:
        conn_string = "mongodb://"

    if (
        config["database"]["admin"] is not None
        and config["database"]["admin_pwd"] is not None
    ):
        conn_string += (
            f"{config['database']['admin']}:{config['database']['admin_pwd']}@"
        )

    conn_string += f"{config['database']['host']}"
    if config["database"].get("srv", False) is not True:
        conn_string += f":{config['database']['port']}"

    added_params = False
    if config["database"].get("replica_set", None) is not None:
        conn_string += f"/?replicaSet={config['database']['replica_set']}"
        added_params = True

    if config["database"].get("max_pool_size", None) is not None:
        if added_params is False:
            conn_string += f"/?maxPoolSize={config['database']['max_pool_size']}"
        else:
            conn_string += f"&maxPoolSize={config['database']['max_pool_size']}"

    client = AsyncIOMotorClient(conn_string)

    mongo = client[config["database"]["db"]]

    # add site admin if necessary
    await add_admin(mongo)

    # add program id 1 if necessary
    await add_master_program(mongo)

    # init app with auth middleware
    app = web.Application(middlewares=[auth_middleware])

    # store mongo connection
    app["mongo"] = mongo

    # indices
    await app["mongo"].sources.create_index(
        [("coordinates.radec_geojson", "2dsphere"), ("_id", 1)], background=True
    )
    await app["mongo"].sources.create_index([("created", -1)], background=True)
    await app["mongo"].sources.create_index([("zvm_program_id", 1)], background=True)
    await app["mongo"].sources.create_index(
        [("zvm_program_id", 1), ("labels.user", 1)], background=True
    )
    await app["mongo"].sources.create_index(
        [("zvm_program_id", 1), ("labels.user", 1), ("_id", 1)], background=True
    )
    await app["mongo"].sources.create_index([("labels.label", 1)], background=True)
    await app["mongo"].sources.create_index([("lc.id", 1)], background=True)

    # graciously close mongo client on shutdown
    async def close_mongo(app):
        app["mongo"].client.close()

    app.on_cleanup.append(close_mongo)

    # Kowalski connection:
    app["kowalski"] = Kowalski(instances=config["kowalski"]["instances"])

    # set up JWT for user authentication/authorization
    app["JWT"] = {
        "JWT_SECRET": config["server"]["JWT_SECRET_KEY"],
        "JWT_ALGORITHM": "HS256",
        "JWT_EXP_DELTA_SECONDS": 30 * 86400 * 3,
    }

    # render templates with jinja2
    aiohttp_jinja2.setup(
        app,
        loader=jinja2.FileSystemLoader(current_dir + "/templates"),
        filters={"tojson_pretty": to_pretty_json},
    )

    # set up browser sessions
    fernet_key = config["misc"]["fernet_key"].encode()
    secret_key = base64.urlsafe_b64decode(fernet_key)
    setup(app, EncryptedCookieStorage(secret_key))

    # route table
    # app.add_routes([web.get('/', hello)])
    app.add_routes(routes)

    # static files
    app.add_routes([web.static("/static", current_dir + "/static")])

    # data files
    app.add_routes([web.static("/data", config["path"].get("path_data", "./data"))])

    return app


""" TODO: Tests """


class TestAPIs(object):
    # python -m pytest -s server.py
    # python -m pytest server.py

    # test user management API for admin
    async def test_users(self, aiohttp_client):
        client = await aiohttp_client(await app_factory())

        login = await client.post(
            "/login",
            json={
                "username": config["server"]["admin_username"],
                "password": config["server"]["admin_password"],
            },
        )
        # print(login)
        assert login.status == 200

        # test = await client.get('/lab/ztf-alerts')
        # print(test)

        # adding a user
        resp = await client.put(
            "/users", json={"user": "test_user", "password": random_alphanumeric_str(6)}
        )
        assert resp.status == 200
        # text = await resp.text()
        # text = await resp.json()

        # editing user credentials
        resp = await client.post(
            "/users",
            json={
                "_user": "test_user",
                "edit-user": "test_user",
                "edit-password": random_alphanumeric_str(6),
            },
        )
        assert resp.status == 200
        resp = await client.post(
            "/users",
            json={
                "_user": "test_user",
                "edit-user": "test_user_edited",
                "edit-password": "",
            },
        )
        assert resp.status == 200

        # deleting a user
        resp = await client.delete("/users", json={"user": "test_user_edited"})
        assert resp.status == 200

    # test programmatic query API
    async def test_query(self, aiohttp_client):
        # todo:
        client = await aiohttp_client(await app_factory())

        # check JWT authorization
        auth = await client.post(
            "/auth",
            json={
                "username": config["server"]["admin_username"],
                "password": config["server"]["admin_password"],
            },
        )
        assert auth.status == 200
        # print(await auth.text())
        # print(await auth.json())
        credentials = await auth.json()
        assert "token" in credentials

        access_token = credentials["token"]

        headers = {"Authorization": access_token}

        collection = "sources"

        # check query without book-keeping
        qu = {
            "query_type": "general_search",
            "query": f"db['{collection}'].find_one({{}}, {{'_id': 1}})",
            "kwargs": {"save": False},
        }
        # print(qu)
        resp = await client.put("/query", json=qu, headers=headers, timeout=1)
        assert resp.status == 200
        result = await resp.json()
        assert result["status"] == "done"

        # check query with book-keeping
        qu = {
            "query_type": "general_search",
            "query": f"db['{collection}'].find_one({{}}, {{'_id': 1}})",
            "kwargs": {"enqueue_only": True, "_id": random_alphanumeric_str(32)},
        }
        # print(qu)
        resp = await client.put("/query", json=qu, headers=headers, timeout=0.15)
        assert resp.status == 200
        result = await resp.json()
        # print(result)
        assert result["status"] == "enqueued"

        # remove enqueued query
        resp = await client.delete(
            "/query", json={"task_id": result["query_id"]}, headers=headers, timeout=1
        )
        assert resp.status == 200
        result = await resp.json()
        assert result["message"] == "success"


if __name__ == "__main__":

    web.run_app(app_factory(), port=config["server"]["port"])
