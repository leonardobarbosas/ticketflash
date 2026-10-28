from app.db import cache

with cache.pubsub() as subscriber:
    subscriber.subscribe('pedidos:confirmados')
    print('Aguardando pedidos confirmados...', flush=True)
    for message in subscriber.listen():
        if message['type'] == 'message':
            print(message['data'], flush=True)
