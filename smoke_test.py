import json
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from uuid import uuid4
from app.db import cache, db

def request(method, path, data=None):
    body = json.dumps(data).encode() if data is not None else None
    req = Request('http://localhost:8000' + path, data=body, method=method,
                  headers={'Content-Type': 'application/json'})
    try:
        with urlopen(req) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())

def run():
    from datetime import datetime, timezone
    uid = str(db.usuarios.insert_one({'nome': 'Teste', 'email': f'{uuid4()}@example.com',
                                     'cidade': 'São Paulo', 'dataCadastro': datetime.now(timezone.utc)}).inserted_id)
    event = {'nome': 'Teste automatizado', 'categoria': 'teste', 'data': '2027-12-01T20:00:00Z',
             'local': {'cidade': 'São Paulo', 'endereco': 'Rua Teste'}, 'atracoes': ['Demo'],
             'setores': [{'nome': 'Pista', 'preco': 10, 'capacidade': 2}]}
    status, created = request('POST', '/eventos', event)
    assert status == 201, created
    eid = created['_id']
    path = f'/eventos/{eid}'
    assert request('GET', path)[1]['origem'] == 'banco'
    assert request('GET', path)[1]['origem'] == 'cache'
    assert 0 < cache.ttl(f'evento:{eid}') <= 60
    event['nome'] = 'Teste atualizado'
    assert request('PUT', path, event)[0] == 200
    assert not cache.exists(f'evento:{eid}')
    assert request('GET', path)[1]['origem'] == 'banco'
    payload = {'usuarioId': uid, 'eventoId': eid, 'setor': 'Pista', 'quantidade': 1}
    status, reservation = request('POST', '/reservas', payload)
    assert status == 201, reservation
    rid = reservation['reservaId']
    assert 0 < cache.ttl(f'reserva:{rid}') <= 600
    assert request('POST', '/reservas', payload)[0] == 409
    confirmation = {'usuarioId': uid, 'reservaId': rid}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: request('POST', '/pedidos/confirmar', confirmation), range(4)))
    assert all(status == 200 for status, _ in results), results
    assert db.pedidos.count_documents({'reservaId': rid}) == 1
    assert request('GET', path)[1]['evento']['setores'][0]['vendidos'] == 1
    status, second = request('POST', '/reservas', payload)
    assert status == 201
    assert request('POST', '/pedidos/confirmar', confirmation)[0] == 200
    assert cache.get(f'bloqueio:{eid}:Pista') == second['reservaId']
    cache.pexpire(f'reserva:{second["reservaId"]}', 1)
    cache.pexpire(f'bloqueio:{eid}:Pista', 1)
    import time
    time.sleep(0.05)
    assert request('POST', '/pedidos/confirmar', {'usuarioId': uid, 'reservaId': second['reservaId']})[0] == 410
    assert db.pedidos.count_documents({'reservaId': second['reservaId']}) == 0
    assert request('POST', '/reservas', {**payload, 'quantidade': 2})[0] == 409
    cache.delete(f'limite:{uid}')
    statuses = [request('POST', '/reservas', {**payload, 'setor': 'inexistente'})[0] for _ in range(11)]
    assert statuses[:10] == [404] * 10 and statuses[10] == 429, statuses
    assert request('GET', '/relatorios/faturamento')[0] == 200
    assert request('GET', f'/relatorios/ocupacao/{eid}')[1][0]['percentual'] == 50
    assert any(row['eventoId'] == eid for row in request('GET', '/eventos/ranking')[1])
    assert request('GET', '/eventos?categoria=teste')[0] == 200
    _, fresh = request('POST', '/eventos', event)
    request('GET', '/eventos/' + fresh['_id'])
    assert request('DELETE', '/eventos/' + fresh['_id'])[0] == 204
    assert not cache.exists('evento:' + fresh['_id'])
    assert request('GET', '/eventos/' + fresh['_id'])[0] == 404
    print('OK: CRUD, cache, TTL, reservas, idempotência concorrente, 410, 429, ranking e relatórios.')

if __name__ == '__main__':
    run()
