from datetime import datetime, timezone, timedelta
from bson import ObjectId
from app.db import db, client, indices

def seed():
    now = datetime.now(timezone.utc)
    with client.start_session() as session:
        def populate(s):
            if db.eventos.count_documents({}, session=s):
                return
            eventos = [{'_id': ObjectId(f'{i+1:024x}'), 'nome': f'TicketFlash Festival {i+1}',
                        'categoria': ['musica', 'teatro'][i % 2], 'data': now + timedelta(days=30+i),
                        'local': {'cidade': ['São Paulo', 'Rio de Janeiro'][i % 2], 'endereco': f'Avenida Central, {i+100}'},
                        'atracoes': [f'Artista {i+1}'],
                        'setores': [{'nome': 'Pista', 'preco': 100.0, 'capacidade': 100, 'vendidos': 5},
                                    {'nome': 'VIP', 'preco': 200.0, 'capacidade': 30, 'vendidos': 0}]} for i in range(10)]
            usuarios = [{'_id': ObjectId(f'{i+101:024x}'), 'nome': f'Usuário {i+1}', 'email': f'usuario{i+1}@example.com',
                         'cidade': 'São Paulo', 'dataCadastro': now} for i in range(20)]
            pedidos = [{'usuarioId': usuarios[i % 20]['_id'], 'eventoId': eventos[i % 10]['_id'],
                        'setor': 'Pista', 'quantidade': 1, 'valorTotal': 100.0, 'status': 'confirmado', 'criadoEm': now} for i in range(50)]
            db.eventos.insert_many(eventos, session=s)
            db.usuarios.insert_many(usuarios, session=s)
            db.pedidos.insert_many(pedidos, session=s)
        session.with_transaction(populate)

if __name__ == '__main__':
    indices()
    seed()
    print('Carga concluída: 10 eventos, 20 usuários e 50 pedidos na primeira execução.')
