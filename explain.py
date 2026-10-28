import json
from app.db import db

plan = db.command('explain', {'find': 'eventos', 'filter': {'categoria': 'musica'},
                            'sort': {'data': 1}, 'hint': 'categoria_data'}, verbosity='executionStats')
print(json.dumps(plan, indent=2, default=str))
