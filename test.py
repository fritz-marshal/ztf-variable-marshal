from zvm import zvm


# for testing, we just run a simple query with the test credentials to see if the server is up
def test_connection():
    z = zvm(
        protocol="http",
        host="0.0.0.0",
        port=8000,
        username="admin",
        password="admin",
    )

    # check connection
    connection = z.check_connection()
    assert connection is True
