from unittest.mock import MagicMock
from bson import ObjectId
from fakeredis import FakeRedis
from fastapi.testclient import TestClient
import pytest
import app.main as main

@pytest.fixture
def setup(monkeypatch):
    redis = FakeRedis(decode_responses=True)
    mongo = MagicMock()
    monkeypatch.setattr(main, 'cache', redis)
    monkeypatch.setattr(main, 'db', mongo)
    return TestClient(main.app), redis, mongo

def document():
    return {'_id': ObjectId(), 'nome': 'Show', 'categoria': 'musica',
            'data': '2027-01-01T00:00:00Z', 'local': {'cidade': 'SP', 'endereco': 'Rua A'},
            'atracoes': ['Banda'], 'setores': [{'nome': 'Pista', 'preco': 10, 'capacidade': 5, 'vendidos': 0}]}

def test_cache_ranking_and_ttl(setup):
    api, redis, mongo = setup
    doc = document()
    mongo.eventos.find_one.return_value = doc
    path = '/eventos/' + str(doc['_id'])
    assert api.get(path).json()['origem'] == 'banco'
    assert api.get(path).json()['origem'] == 'cache'
    assert mongo.eventos.find_one.call_count == 1
    assert 0 < redis.ttl('evento:' + str(doc['_id'])) <= 60
    assert api.get('/eventos/ranking').json()[0]['visualizacoes'] == 2

def test_reservation_nx_ttl_and_rate_limit(setup):
    api, redis, mongo = setup
    doc = document()
    mongo.eventos.find_one.return_value = doc
    payload = {'usuarioId': str(ObjectId()), 'eventoId': str(doc['_id']), 'setor': 'Pista', 'quantidade': 1}
    first = api.post('/reservas', json=payload)
    assert first.status_code == 201
    assert 0 < redis.ttl('reserva:' + first.json()['reservaId']) <= 600
    for _ in range(9):
        assert api.post('/reservas', json=payload).status_code == 409
    response = api.post('/reservas', json=payload)
    assert response.status_code == 429
    assert int(response.headers['Retry-After']) > 0

def test_expired_does_not_write(setup):
    api, redis, mongo = setup
    mongo.pedidos.find_one.return_value = None
    response = api.post('/pedidos/confirmar', json={'usuarioId': str(ObjectId()), 'reservaId': 'expired'})
    assert response.status_code == 410
    mongo.pedidos.insert_one.assert_not_called()
    mongo.eventos.update_one.assert_not_called()

def test_idempotent_retry_keeps_new_hold(setup):
    api, redis, mongo = setup
    uid, eid = ObjectId(), ObjectId()
    mongo.pedidos.find_one.return_value = {'_id': ObjectId(), 'usuarioId': uid, 'eventoId': eid, 'setor': 'Pista'}
    key = f'bloqueio:{eid}:Pista'
    redis.set(key, 'new-reservation', ex=600)
    assert api.post('/pedidos/confirmar', json={'usuarioId': str(uid), 'reservaId': 'old-reservation'}).status_code == 200
    assert redis.get(key) == 'new-reservation'
    mongo.pedidos.insert_one.assert_not_called()

def test_crud_invalidation(setup):
    api, redis, mongo = setup
    doc = document()
    mongo.eventos.find_one.return_value = doc
    path = '/eventos/' + str(doc['_id'])
    key = 'evento:' + str(doc['_id'])
    body = {k: v for k, v in doc.items() if k != '_id'}
    redis.set(key, 'stale')
    assert api.put(path, json=body).status_code == 200
    assert not redis.exists(key)
    mongo.pedidos.find_one.return_value = None
    mongo.eventos.delete_one.return_value.deleted_count = 1
    redis.set(key, 'stale')
    assert api.delete(path).status_code == 204
    assert not redis.exists(key)

def test_capacity_and_validation(setup):
    api, redis, mongo = setup
    doc = document()
    doc['setores'][0]['vendidos'] = 5
    mongo.eventos.find_one.return_value = doc
    payload = {'usuarioId': str(ObjectId()), 'eventoId': str(doc['_id']), 'setor': 'Pista', 'quantidade': 1}
    assert api.post('/reservas', json=payload).status_code == 409
    assert api.post('/reservas', json={**payload, 'quantidade': 0}).status_code == 422
    assert api.get('/eventos/not-an-id').status_code == 422
