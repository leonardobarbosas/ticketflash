import time
import subprocess
from pymongo import MongoClient
from pymongo.errors import OperationFailure, PyMongoError

direct = MongoClient('mongodb://mongo:27017/?directConnection=true', serverSelectionTimeoutMS=3000)
try:
    direct.admin.command('replSetInitiate', {'_id': 'rs0', 'members': [{'_id': 0, 'host': 'mongo:27017'}]})
except OperationFailure as exc:
    if exc.code != 23:
        raise
for attempt in range(60):
    try:
        if direct.admin.command('hello').get('isWritablePrimary'):
            break
    except PyMongoError:
        pass
    time.sleep(1)
else:
    raise RuntimeError('MongoDB não elegeu um primário')
from app.db import indices
from seed import seed
indices()
seed()
subprocess.run(['uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000'], check=True)
