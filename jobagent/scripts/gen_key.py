"""Print a fresh Fernet key for ENCRYPTION_KEY."""
from cryptography.fernet import Fernet

print(Fernet.generate_key().decode())
