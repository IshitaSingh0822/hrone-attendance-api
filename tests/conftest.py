import os

# Always use a throwaway database so the tests can never wipe real data.
os.environ["MONGO_DB"] = "attendance_test"
