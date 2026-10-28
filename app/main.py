import json
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from bson import ObjectId
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from redis.exceptions import LockError, RedisError
from pymongo.errors import PyMongoError
from app.db import db, cache, client

app = FastAPI(title='TicketFlash', version='1.0.0')

def serial(value):
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: serial(v) for k, v in value.items()}
    if isinstance(value, list):
        return [serial(v) for v in value]
    return value

def oid(value):
    if not ObjectId.is_valid(value):
        raise HTTPException(422, 'ID MongoDB inválido')
    return ObjectId(value)

@app.exception_handler(RedisError)
@app.exception_handler(PyMongoError)
def unavailable(request, exc):
    return JSONResponse(status_code=503, content={'detail': 'Banco temporariamente indisponível; tente novamente.'})

@contextmanager
def guard():
    lock = cache.lock('ticketflash:operacao', timeout=None, blocking_timeout=5)
    if not lock.acquire():
        raise HTTPException(503, 'Operação em andamento; tente novamente.')
    try:
        yield
    finally:
        lock.release()

class Local(BaseModel):
    cidade: str = Field(min_length=1)
    endereco: str = Field(min_length=1)

class Setor(BaseModel):
    nome: str = Field(min_length=1)
    preco: float = Field(gt=0, allow_inf_nan=False)
    capacidade: int = Field(gt=0)

class Evento(BaseModel):
    nome: str = Field(min_length=1)
    categoria: str = Field(min_length=1)
    data: datetime
    local: Local
    atracoes: list[str]
    setores: list[Setor] = Field(min_length=1)

    @field_validator('setores')
    @classmethod
    def unique_names(cls, value):
        if len({s.nome for s in value}) != len(value):
            raise ValueError('Nomes dos setores devem ser únicos')
        return value

class Reserva(BaseModel):
    usuarioId: str
    eventoId: str
    setor: str = Field(min_length=1)
    quantidade: int = Field(gt=0, le=100)

class Confirmacao(BaseModel):
    reservaId: str = Field(min_length=1)
    usuarioId: str

def event_doc(body, previous=None):
    doc = body.model_dump()
    old = {s['nome']: s for s in previous['setores']} if previous else {}
    names = {s['nome'] for s in doc['setores']}
    if any(s['vendidos'] > 0 and name not in names for name, s in old.items()):
        raise HTTPException(409, 'Não é possível remover setor com vendas')
    for s in doc['setores']:
        s['vendidos'] = old.get(s['nome'], {}).get('vendidos', 0)
        if s['capacidade'] < s['vendidos']:
            raise HTTPException(409, 'Capacidade menor que as vendas existentes')
    return doc

def check_holds(event_id):
    if next(cache.scan_iter(match=f'bloqueio:{event_id}:*'), None):
        raise HTTPException(409, 'Evento possui uma reserva ativa')

@app.get('/health')
def health():
    db.command('ping')
    cache.ping()
    return {'status': 'ok'}

@app.get('/usuarios')
def usuarios():
    return serial(list(db.usuarios.find({}, {'nome': 1, 'email': 1}).limit(100)))

@app.post('/eventos', status_code=201)
def criar(body: Evento):
    with guard():
        doc = event_doc(body)
        db.eventos.insert_one(doc)
        return serial(doc)

@app.get('/eventos')
def listar(categoria: str | None = None, cidade: str | None = None):
    filters = {}
    if categoria:
        filters['categoria'] = categoria
    if cidade:
        filters['local.cidade'] = cidade
    return serial(list(db.eventos.find(filters).sort('data', 1).limit(100)))

@app.get('/eventos/ranking')
def ranking():
    return [{'eventoId': eid, 'visualizacoes': int(score)} for eid, score in cache.zrevrange('eventos:ranking', 0, 4, withscores=True)]

@app.get('/eventos/{evento_id}')
def buscar(evento_id: str):
    event_id = oid(evento_id)
    with guard():
        key = f'evento:{event_id}'
        cached = cache.get(key)
        if cached:
            data, origem = json.loads(cached), 'cache'
        else:
            doc = db.eventos.find_one({'_id': event_id})
            if not doc:
                raise HTTPException(404, 'Evento não encontrado')
            data, origem = serial(doc), 'banco'
            cache.set(key, json.dumps(data), ex=60)
        cache.zincrby('eventos:ranking', 1, str(event_id))
        return {'origem': origem, 'evento': data}

@app.put('/eventos/{evento_id}')
def atualizar(evento_id: str, body: Evento):
    eid = oid(evento_id)
    with guard():
        old = db.eventos.find_one({'_id': eid})
        if not old:
            raise HTTPException(404, 'Evento não encontrado')
        check_holds(eid)
        doc = event_doc(body, old)
        db.eventos.update_one({'_id': eid}, {'$set': doc})
        cache.delete(f'evento:{eid}')
        return serial({'_id': eid, **doc})

@app.delete('/eventos/{evento_id}', status_code=204)
def remover(evento_id: str):
    eid = oid(evento_id)
    with guard():
        check_holds(eid)
        if db.pedidos.find_one({'eventoId': eid}):
            raise HTTPException(409, 'Evento com pedidos não pode ser removido')
        if not db.eventos.delete_one({'_id': eid}).deleted_count:
            raise HTTPException(404, 'Evento não encontrado')
        cache.delete(f'evento:{eid}')
        cache.zrem('eventos:ranking', str(eid))

@app.get('/relatorios/faturamento')
def faturamento():
    return serial(list(db.pedidos.aggregate([
        {'$match': {'status': 'confirmado'}},
        {'$group': {'_id': '$eventoId', 'faturamento': {'$sum': '$valorTotal'}}},
        {'$lookup': {'from': 'eventos', 'localField': '_id', 'foreignField': '_id', 'as': 'evento'}},
        {'$unwind': '$evento'},
        {'$project': {'eventoId': '$_id', '_id': 0, 'nome': '$evento.nome', 'faturamento': 1}},
        {'$sort': {'faturamento': -1}}
    ])))

@app.get('/relatorios/ocupacao/{evento_id}')
def ocupacao(evento_id: str):
    result = list(db.eventos.aggregate([
        {'$match': {'_id': oid(evento_id)}}, {'$unwind': '$setores'},
        {'$project': {'_id': 0, 'setor': '$setores.nome', 'vendidos': '$setores.vendidos',
                      'capacidade': '$setores.capacidade', 'percentual': {'$multiply': [
                          {'$divide': ['$setores.vendidos', '$setores.capacidade']}, 100]}}}
    ]))
    if not result:
        raise HTTPException(404, 'Evento não encontrado')
    return result

RATE = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], 60) end
return {n, redis.call('TTL', KEYS[1])}
"""

@app.post('/reservas', status_code=201)
def reservar(body: Reserva):
    uid, eid = oid(body.usuarioId), oid(body.eventoId)
    n, ttl = cache.eval(RATE, 1, f'limite:{uid}')
    if n > 10:
        raise HTTPException(429, 'Limite de 10 tentativas de reserva por minuto', headers={'Retry-After': str(max(1, ttl))})
    with guard():
        if not db.usuarios.find_one({'_id': uid}):
            raise HTTPException(404, 'Usuário não encontrado')
        event = db.eventos.find_one({'_id': eid})
        if not event:
            raise HTTPException(404, 'Evento não encontrado')
        sector = next((s for s in event['setores'] if s['nome'] == body.setor), None)
        if not sector:
            raise HTTPException(404, 'Setor não encontrado')
        if sector['vendidos'] + body.quantidade > sector['capacidade']:
            raise HTTPException(409, 'Capacidade insuficiente')
        rid = str(uuid4())
        hold = f'bloqueio:{eid}:{body.setor}'
        payload = {**body.model_dump(), 'usuarioId': str(uid), 'eventoId': str(eid),
                   'reservaId': rid, 'bloqueio': hold,
                   'valorTotal': float((Decimal(str(sector['preco'])) * body.quantidade).quantize(Decimal('0.01')))}
        ok = cache.eval("""
            if not redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', 600) then return 0 end
            redis.call('SET', KEYS[2], ARGV[2], 'NX', 'EX', 600)
            return 1
        """, 2, hold, f'reserva:{rid}', rid, json.dumps(payload))
        if not ok:
            raise HTTPException(409, 'Setor temporariamente reservado')
        return {'reservaId': rid, 'expiraEmSegundos': 600, 'valorTotal': payload['valorTotal']}

@app.post('/pedidos/confirmar')
def confirmar(body: Confirmacao):
    uid = oid(body.usuarioId)
    with guard():
        existing = db.pedidos.find_one({'reservaId': body.reservaId})
        if existing:
            if existing['usuarioId'] != uid:
                raise HTTPException(403, 'Reserva pertence a outro usuário')
            cache.delete(f'evento:{existing["eventoId"]}')
            cache.delete(f'reserva:{body.reservaId}')
            cache.eval("if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0",
                       1, f'bloqueio:{existing["eventoId"]}:{existing["setor"]}', body.reservaId)
            return serial(existing)
        raw = cache.get(f'reserva:{body.reservaId}')
        if not raw:
            raise HTTPException(410, 'Reserva expirada ou inexistente')
        r = json.loads(raw)
        if r['usuarioId'] != str(uid):
            raise HTTPException(403, 'Reserva pertence a outro usuário')
        eid = oid(r['eventoId'])
        order = {'reservaId': body.reservaId, 'usuarioId': uid, 'eventoId': eid,
                 'setor': r['setor'], 'quantidade': r['quantidade'], 'valorTotal': r['valorTotal'],
                 'status': 'confirmado', 'criadoEm': datetime.now(timezone.utc)}
        def purchase(session):
            available = {'$and': [
                {'$eq': ['$$s.nome', r['setor']]},
                {'$lte': [{'$add': ['$$s.vendidos', r['quantidade']]}, '$$s.capacidade']}
            ]}
            result = db.eventos.update_one({
                '_id': eid, 'setores': {'$elemMatch': {'nome': r['setor']}},
                '$expr': {'$anyElementTrue': [{'$map': {'input': '$setores', 'as': 's', 'in': available}}]}
            }, {'$inc': {'setores.$.vendidos': r['quantidade']}}, session=session)
            if not result.modified_count:
                raise HTTPException(409, 'Capacidade insuficiente ou evento removido')
            db.pedidos.insert_one(order, session=session)
        with client.start_session() as session:
            session.with_transaction(purchase)
        cache.delete(f'evento:{eid}')
        cache.delete(f'reserva:{body.reservaId}', r['bloqueio'])
        cache.publish('pedidos:confirmados', json.dumps(serial(order)))
        return serial(order)
