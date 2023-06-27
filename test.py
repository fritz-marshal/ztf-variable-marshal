from zvm import zvm


# for testing, we just run a simple query with the test credentials to see if the server is up
def test_connection():
    z = zvm(
        protocol="http",
        host="localhost",
        port=4000,
        username="admin",
        password="admin",
    )

    # check connection
    assert z.check_connection()
