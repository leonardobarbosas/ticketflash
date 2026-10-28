import os
from pymongo import MongoClient, ASCENDING
from redis import Redis

client = MongoClient(os.getenv('MONGO_URL', 'mongodb://localhost:27017/?replicaSet=rs0'), serverSelectionTimeoutMS=5000)
db = client.ticketflash
cache = Redis.from_url(os.getenv('REDIS_URL', 'redis://localhost:6379/0'), decode_responses=True)

def indices():
    db.usuarios.create_index('email', unique=True)
    db.eventos.create_index([('categoria', ASCENDING), ('data', ASCENDING)], name='categoria_data')
    db.pedidos.create_index('usuarioId')
    db.pedidos.create_index('reservaId', unique=True, sparse=True)
