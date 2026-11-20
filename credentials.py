"""Local encrypted SQLite credentials. The master key isn't in SQLite."""
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CredentialStoreError(Exception):
    pass


class Credentials:
    def __init__(self, db_path):
        db_path=Path(db_path)
        self.path=db_path.with_name(db_path.name+".credentials.key")

    def _cipher(self, create=False):
        try:
            try:
                fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                except FileExistsError:
                    return self._cipher()
                key=Fernet.generate_key()
                with os.fdopen(fd,"wb") as f:
                    f.write(key);f.flush();os.fsync(f.fileno())
                return Fernet(key)
            with os.fdopen(fd,"rb") as f:
                status=os.fstat(f.fileno())
                if not stat.S_ISREG(status.st_mode) or status.st_mode&0o077:
                    raise CredentialStoreError("Key 加密文件权限不安全，请将权限设为 600。")
                return Fernet(f.read(128))
        except (OSError,ValueError) as exc:
            raise CredentialStoreError("无法读取 Key 加密文件。请恢复原文件，或清除已保存的 Key 后重新填写。") from None

    def load(self, con):
        rows=con.execute("SELECT name,ciphertext FROM credential_values").fetchall()
        if not rows:
            return {}
        cipher=self._cipher()
        try:
            return {name:cipher.decrypt(blob).decode("utf-8") for name,blob in rows}
        except (InvalidToken,TypeError,UnicodeError):
            raise CredentialStoreError("数据库中的 Key 无法解密。请恢复原加密文件，或清除已保存的 Key 后重新填写。") from None

    def save(self,con,values):
        selected={name:value.strip() for name,value in values.items() if value is not None}
        if not selected:
            return
        for name,value in selected.items():
            if not value:
                con.execute("DELETE FROM credential_values WHERE name=?",(name,))
        if any(selected.values()):
            existing=con.execute("SELECT 1 FROM credential_values LIMIT 1").fetchone()
            cipher=self._cipher(create=not existing)
            # Avoid overwriting stored credentials if this database was paired with the wrong key.
            if existing:
                self.load(con)
            con.executemany("""INSERT INTO credential_values VALUES (?,?)
                   ON CONFLICT(name) DO UPDATE SET ciphertext=excluded.ciphertext""",
                   [(name,cipher.encrypt(value.encode("utf-8"))) for name,value in selected.items() if value])
