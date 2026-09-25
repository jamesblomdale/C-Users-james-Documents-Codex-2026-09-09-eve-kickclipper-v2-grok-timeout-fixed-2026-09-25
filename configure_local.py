"""Set up a new computer without overwriting existing settings."""
from pathlib import Path
import secrets
from dotenv import dotenv_values, set_key

root=Path(__file__).resolve().parent
path=root/'.env'
if not path.exists():path.write_bytes((root/'.env.example').read_bytes())
values=dotenv_values(path)
for key,value in {'SECRET_KEY':secrets.token_hex(32),'OWNER_EMAIL':'admin@kickclipper.local',
                  'OWNER_PASSWORD':secrets.token_urlsafe(18),'DEMO_MODE':'1'}.items():
    if not values.get(key):set_key(str(path),key,value)
print('Settings ready in .env. Add your provider API key before processing.')
print('Your admin email and generated password are in .env. Keep that file private.')
