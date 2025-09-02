# save as test_native.py
import iris, ssl, os
HOST=os.getenv("IRIS_HOST","127.0.0.1")
PORT=int(os.getenv("IRIS_PORT","1972"))
NS=os.getenv("IRIS_NAMESPACE","DEMO")
USR=os.getenv("IRIS_USERNAME","_SYSTEM")
PWD=os.getenv("IRIS_PASSWORD","ISCDEMO")

# If your IRIS SuperServer requires TLS, configure an SSL context:
ctx = None
# ctx = ssl.create_default_context(cafile="/path/to/ca.pem")
# ctx.check_hostname = False  # self-signed/local CN? Uncomment if needed.

print("Connecting to", HOST, PORT, NS)
conn = iris.connect(HOST, PORT, NS, USR, PWD, timeout=5)
print("Connected OK")
conn.close()
print("Closed")
