#!/usr/bin/env python3
r"""Garimpo: explorador local de backups do iPhone (não criptografados).

Lê o backup que o iTunes / app Dispositivos Apple salvou no computador e mostra Fotos, WhatsApp,
Notas, Contatos e os arquivos de qualquer app. Tudo roda na sua máquina (127.0.0.1) e o backup
original nunca é alterado.

Uso:
    python garimpo.py                          # abre a interface
    python garimpo.py --janela                 # janela própria, sem console (usado pelos atalhos)
    python garimpo.py --criar-atalhos          # atalhos com ícone no Windows (Área de Trabalho/Menu Iniciar)
    python garimpo.py --backup-dir D:\Backups  # pasta com os backups (se não for a padrão)
    python garimpo.py --export-photos D:\Fotos [--jpg] [--backup ID]
    python garimpo.py --export-whatsapp D:\WhatsApp [--app wa|biz|ambos]

Suas pastas, exclusões e favoritas ficam em %APPDATA%\Garimpo (ou --dados), fora deste projeto.
HEIC -> JPG precisa de: python -m pip install pillow pillow-heif
"""
import argparse
import atexit
import base64
import collections
import datetime as dt
import gzip
import html
import io
import json
import mimetypes
import os
import pathlib
import plistlib
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import pillow_heif
    from PIL import Image
    pillow_heif.register_heif_opener()
    HEIC_OK = True
except Exception:
    HEIC_OK = False

APPLE_EPOCH = 978307200  # 2001-01-01 em Unix time
CAMERA = "CameraRollDomain"
WA_DOMAINS = {
    "wa": "AppDomainGroup-group.net.whatsapp.WhatsApp.shared",
    "biz": "AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared",
}
APP_NAMES = {"wa": "WhatsApp", "biz": "WhatsApp Business"}
CTYPES = {
    ".heic": "image/heic", ".heif": "image/heif", ".mov": "video/mp4", ".mp4": "video/mp4",
    ".m4v": "video/mp4", ".opus": "audio/ogg", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
    ".webp": "image/webp", ".thumb": "image/jpeg", ".mmsthumb": "image/jpeg", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".pdf": "application/pdf",
}
BUILTIN_FOLDERS = [
    {"id": "entrada", "name": "Sem pasta"},
    {"id": "importantes", "name": "Importantes"},
    {"id": "inuteis", "name": "Inúteis"},
    {"id": "grupos", "name": "Grupos"},
]
MEDIA_CATS = {1: "foto", 38: "foto", 2: "video", 11: "video", 39: "video", 3: "audio", 53: "audio",
              8: "doc", 15: "figurinha"}
TYPE_LABELS = {6: "<evento do grupo>", 10: "<mensagem de sistema>", 14: "<mensagem apagada>"}
PREVIEW_LABELS = {1: "📷 Foto", 2: "🎥 Vídeo", 3: "🎤 Áudio", 4: "👤 Contato", 5: "📍 Localização", 8: "📄 Documento",
                  11: "GIF", 14: "🚫 Mensagem apagada", 15: "Figurinha", 38: "📷 Foto (visualização única)",
                  39: "🎥 Vídeo (visualização única)", 53: "🎤 Áudio (visualização única)"}


def apple_ts(v):
    return None if v is None else v + APPLE_EPOCH


# Arquivos do backup nunca devem ser interpretados pelo navegador como página (um HTML/SVG de dentro do
# backup rodaria scripts na mesma origem do app e poderia ler tudo): viram download genérico.
UNSAFE_TYPES = {"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml",
                "text/javascript", "application/javascript", "application/x-javascript", "text/x-javascript",
                "application/ecmascript", "text/ecmascript"}


def ctype_for(name):
    ext = os.path.splitext(name)[1].lower()
    ctype = CTYPES.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return "application/octet-stream" if ctype.lower() in UNSAFE_TYPES else ctype


def norm(s):
    """Minúsculas e sem acentos, para busca."""
    if not s:
        return ""
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def norm_with_map(s):
    out, idx = [], []
    for i, ch in enumerate(s):
        for c in norm(ch):
            out.append(c)
            idx.append(i)
    return "".join(out), idx


def safe_name(s, maxlen=80):
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", s or "").strip(" .")
    return (s or "sem nome")[:maxlen]


def out_dir(p):
    """Pasta de destino absoluta; no Windows usa o prefixo \\\\?\\ para aceitar caminhos > 260 caracteres."""
    p = os.path.abspath(p)
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        p = "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p
    return pathlib.Path(p)


def msg_key(jid, stanza, pk):
    # chave estável entre backups: JID da conversa + ID da mensagem no WhatsApp
    return f"{jid}|{stanza or 'pk' + str(pk)}"


def jid_label(jid):
    if not jid:
        return "Desconhecido"
    num = jid.split("@")[0]
    return "+" + num if jid.endswith("@s.whatsapp.net") else num


def default_roots():
    home = pathlib.Path.home()
    roots = [home / "Apple" / "MobileSync" / "Backup",
             home / "Library" / "Application Support" / "MobileSync" / "Backup"]
    if os.environ.get("APPDATA"):
        roots.append(pathlib.Path(os.environ["APPDATA"]) / "Apple Computer" / "MobileSync" / "Backup")
    return roots


def default_data_dir():
    """Onde ficam pastas, exclusões e favoritas: fora do projeto, para nunca irem parar no Git."""
    if os.name == "nt":
        base = pathlib.Path(os.environ.get("APPDATA") or pathlib.Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = pathlib.Path.home() / "Library" / "Application Support"
    else:
        base = pathlib.Path(os.environ.get("XDG_DATA_HOME") or pathlib.Path.home() / ".local" / "share")
    return base / "Garimpo"


def connect_ro(path):
    # immutable=1: o SQLite nunca tenta escrever nem criar arquivos -wal/-shm no backup
    return sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


# --- Organização (pastas, exclusões, favoritas) -------------------------------
class State:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.lock = threading.RLock()
        self.data = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}

    def app(self, app):
        d = self.data.setdefault(app, {})
        for k, v in (("folders", []), ("chat_folder", {}), ("deleted_chats", {}), ("deleted_msgs", {}), ("stars", {})):
            d.setdefault(k, v)
        return d

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.path)

    def folders(self, app):
        return [dict(f, builtin=True) for f in BUILTIN_FOLDERS] + [dict(f, builtin=False) for f in self.app(app)["folders"]]

    def apply(self, app, a):
        with self.lock:
            st = self.app(app)
            op = a.get("op")
            result = {}
            if op == "move":
                valid = {f["id"] for f in self.folders(app)}
                if a["folder"] != "auto" and a["folder"] not in valid:
                    raise ValueError("Pasta inválida")
                for jid in a["chats"]:
                    if a["folder"] == "auto":
                        st["chat_folder"].pop(jid, None)
                    else:
                        st["chat_folder"][jid] = a["folder"]
            elif op == "delete_chats":
                for jid in a["chats"]:
                    st["deleted_chats"][jid] = {"keep_starred": bool(a.get("keep_starred")), "at": int(time.time())}
            elif op == "restore_chats":
                for jid in a["chats"]:
                    st["deleted_chats"].pop(jid, None)
            elif op == "delete_msgs":
                for k in a["keys"]:
                    st["deleted_msgs"][k] = int(time.time())
            elif op == "restore_msgs":
                for k in a["keys"]:
                    st["deleted_msgs"].pop(k, None)
            elif op == "star":
                st["stars"][a["key"]] = bool(a["value"])
            elif op == "folder_create":
                name = (a.get("name") or "").strip()[:60]
                if not name:
                    raise ValueError("Nome vazio")
                fid = f"c{int(time.time() * 1000)}"
                st["folders"].append({"id": fid, "name": name})
                result["id"] = fid
            elif op == "folder_rename":
                for f in st["folders"]:
                    if f["id"] == a["id"]:
                        f["name"] = (a.get("name") or f["name"]).strip()[:60]
            elif op == "folder_delete":
                st["folders"] = [f for f in st["folders"] if f["id"] != a["id"]]
                for jid in [j for j, f in st["chat_folder"].items() if f == a["id"]]:
                    del st["chat_folder"][jid]
            else:
                raise ValueError("Operação desconhecida")
            self.save()
            return result


STATE = None


class Vis:
    """Regras de visibilidade de mensagens segundo a organização salva."""

    def __init__(self, app):
        st = STATE.app(app)
        self.dm, self.dc, self.stars = st["deleted_msgs"], st["deleted_chats"], st["stars"]

    def fav(self, key, base):
        v = self.stars.get(key)
        return bool(base) if v is None else v

    def chat_ok(self, jid, fav, mode):
        if mode == "all":
            return True
        if mode == "starred":
            return fav
        c = self.dc.get(jid)
        return (bool(c.get("keep_starred")) and fav) if c else True


def auto_folder(c):
    if c["count"] <= 1:
        return "inuteis"
    if c["group"]:
        return "grupos"
    return "entrada"


# --- Backup -------------------------------------------------------------------
MSG_SQL = """
    select m.Z_PK, m.ZISFROMME, m.ZMESSAGEDATE, m.ZTEXT, m.ZMESSAGETYPE, m.ZFROMJID, m.ZSTARRED, m.ZSTANZAID,
           mi.Z_PK, mi.ZMEDIALOCALPATH, mi.ZTHUMBNAILLOCALPATH, mi.ZTITLE, mi.ZFILESIZE,
           mi.ZMOVIEDURATION, mi.ZLATITUDE, mi.ZLONGITUDE, mi.ZVCARDNAME,
           gm.ZCONTACTNAME, gm.ZFIRSTNAME, gm.ZMEMBERJID
    from ZWAMESSAGE m
    left join ZWAMEDIAITEM mi on mi.Z_PK = m.ZMEDIAITEM
    left join ZWAGROUPMEMBER gm on gm.Z_PK = m.ZGROUPMEMBER
    where m.ZCHATSESSION = ?
    order by m.ZMESSAGEDATE {o}, m.Z_PK {o}
    limit ? offset ?"""


class Backup:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.id = self.path.name
        self.info = plistlib.loads((self.path / "Info.plist").read_bytes())
        manifest = plistlib.loads((self.path / "Manifest.plist").read_bytes())
        self.encrypted = bool(manifest.get("IsEncrypted"))
        self._lock = threading.Lock()
        self._index = {}
        self._cache = {}
        self._key_locks = {}

    def summary(self):
        date = self.info.get("Last Backup Date")
        return {
            "id": self.id,
            "name": self.info.get("Device Name"),
            "model": self.info.get("Product Type"),
            "ios": self.info.get("Product Version"),
            "date": date.isoformat() if date else None,
            "encrypted": self.encrypted,
            "heic": HEIC_OK,
            "apps": [k for k, d in WA_DOMAINS.items() if not self.encrypted and self.file(d, "ChatStorage.sqlite")],
            "archived": self.is_archived,
            "notes": not self.encrypted and self.has(NOTES_DOM, "NoteStore.sqlite"),
            "contacts": not self.encrypted and self.has("HomeDomain", AB_DB),
        }

    @property
    def is_archived(self):
        """Backup arquivado pelo iTunes/Dispositivos Apple (<UDID>-AAAAMMDD-HHMMSS)."""
        return bool(re.search(r"-\d{8}-\d{6}$", self.id))

    # --- Manifest.db: (domínio, caminho) -> arquivo físico -------------------
    def _domain_index(self, domain):
        with self._lock:
            if domain not in self._index:
                db = connect_ro(self.path / "Manifest.db")
                self._index[domain] = dict(db.execute(
                    "select relativePath, fileID from Files where domain=? and flags=1", (domain,)))
                db.close()
            return self._index[domain]

    def file(self, domain, rel):
        fid = self._domain_index(domain).get(rel)
        if not fid:
            return None
        p = self.path / fid[:2] / fid
        return p if p.exists() else None

    def file_size(self, domain, rel):
        """(caminho, tamanho) com um único acesso ao disco, ou (None, 0)."""
        fid = self._domain_index(domain).get(rel)
        if not fid:
            return None, 0
        p = os.path.join(self.path, fid[:2], fid)
        try:
            return p, os.stat(p).st_size
        except OSError:
            return None, 0

    def has(self, domain, rel):
        return rel in self._domain_index(domain)

    def cached(self, key, fn):
        # um lock por chave: quem chega enquanto o cálculo roda espera em vez de recalcular
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            klock = self._key_locks.setdefault(key, threading.Lock())
        with klock:
            with self._lock:
                if key in self._cache:
                    return self._cache[key]
            val = fn()
            with self._lock:
                self._cache[key] = val
            return val

    # --- Fotos ----------------------------------------------------------------
    def photos(self):
        return self.cached("photos", self._load_photos)

    def _load_photos(self):
        p = self.file(CAMERA, "Media/PhotoData/Photos.sqlite")
        if not p:
            return []
        db = connect_ro(p)
        tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
        t = "ZASSET" if "ZASSET" in tables else "ZGENERICASSET"
        cols = {r[1] for r in db.execute(f"pragma table_info({t})")}
        opt = lambda c: c if c in cols else "NULL"
        rows = db.execute(f"""
            select Z_PK, ZDIRECTORY, ZFILENAME, ZKIND, ZDATECREATED, {opt('ZFAVORITE')}, {opt('ZHIDDEN')},
                   {opt('ZWIDTH')}, {opt('ZHEIGHT')}, {opt('ZDURATION')}
            from {t} where coalesce({opt('ZTRASHEDSTATE')}, 0) = 0
            order by ZDATECREATED desc""").fetchall()
        db.close()
        out = []
        for pk, d, f, kind, date, fav, hidden, w, h, dur in rows:
            if not d or not f:
                continue
            out.append({
                "id": pk, "name": f, "video": kind == 1, "date": apple_ts(date),
                "fav": bool(fav), "hidden": bool(hidden), "w": w, "h": h,
                "dur": round(dur) if dur else None,
                "has": self.file(CAMERA, f"Media/{d}/{f}") is not None,
                "_orig": f"Media/{d}/{f}",
                "_thumb": f"Media/PhotoData/Thumbnails/V2/{d}/{f}/5005.JPG",
            })
        return out

    def photo(self, pk):
        idx = self.cached("photo_idx", lambda: {p["id"]: p for p in self.photos()})
        return idx.get(pk)

    # --- WhatsApp: dados base (imutáveis, em cache) ---------------------------
    def wa_db(self, app):
        p = self.file(WA_DOMAINS[app], "ChatStorage.sqlite")
        if not p:
            raise KeyError("WhatsApp não encontrado neste backup")
        return connect_ro(p)

    def pushnames(self, app):
        def load():
            db = self.wa_db(app)
            names = dict(db.execute("select ZJID, ZPUSHNAME from ZWAPROFILEPUSHNAME where ZPUSHNAME is not null"))
            db.close()
            return names
        return self.cached(("push", app), load)

    def wa_meta(self, app):
        """(conversas por Z_PK, chaves das mensagens com estrela no WhatsApp por conversa)"""
        def load():
            db = self.wa_db(app)
            counts = dict(db.execute("select ZCHATSESSION, count(*) from ZWAMESSAGE group by ZCHATSESSION"))
            # ZLASTMESSAGETEXT vem codificado; a prévia sai da própria última mensagem
            rows = db.execute("""
                select s.Z_PK, s.ZPARTNERNAME, s.ZCONTACTJID, s.ZLASTMESSAGEDATE, m.ZTEXT, m.ZMESSAGETYPE
                from ZWACHATSESSION s left join ZWAMESSAGE m on m.Z_PK = s.ZLASTMESSAGE
                where coalesce(s.ZREMOVED, 0) = 0 and coalesce(s.ZCONTACTJID, '') not in ('', 'status@broadcast')
                order by s.ZLASTMESSAGEDATE desc""").fetchall()
            push = self.pushnames(app)
            chats = {}
            for pk, name, jid, date, text, mtype in rows:
                if counts.get(pk):
                    chats[pk] = {"id": pk, "jid": jid, "name": name or push.get(jid) or jid_label(jid),
                                 "group": jid.endswith("@g.us"), "date": apple_ts(date),
                                 "last": text or PREVIEW_LABELS.get(mtype, ""), "count": counts[pk]}
            starred = collections.defaultdict(set)
            for cpk, pk, stz in db.execute("select ZCHATSESSION, Z_PK, ZSTANZAID from ZWAMESSAGE where ZSTARRED=1"):
                if cpk in chats:
                    starred[cpk].add(msg_key(chats[cpk]["jid"], stz, pk))
            db.close()
            return chats, dict(starred)
        return self.cached(("meta", app), load)

    def wa_media_all(self, app):
        """Todas as mídias presentes fisicamente no backup, com o tamanho real do arquivo."""
        def load():
            chats, _ = self.wa_meta(app)
            db = self.wa_db(app)
            rows = db.execute("""
                select mi.Z_PK, m.Z_PK, m.ZCHATSESSION, m.ZMESSAGETYPE, m.ZMESSAGEDATE, m.ZSTANZAID, m.ZSTARRED,
                       mi.ZMEDIALOCALPATH, mi.ZTHUMBNAILLOCALPATH, mi.ZTITLE, mi.ZMOVIEDURATION
                from ZWAMESSAGE m join ZWAMEDIAITEM mi on mi.Z_PK = m.ZMEDIAITEM
                where mi.ZMEDIALOCALPATH is not null""").fetchall()
            db.close()
            dom = WA_DOMAINS[app]
            rel_of = lambda r: next((c for c in (f"Message/{r.lstrip('/')}", r.lstrip("/")) if self.has(dom, c)), None) if r else None
            out = []
            for mid, pk, cpk, t, date, stz, star, mp, tp, title, dur in rows:
                c = chats.get(cpk)
                rel = rel_of(mp) if c else None
                full, size = self.file_size(dom, rel) if rel else (None, 0)
                if not full:
                    continue
                out.append({"id": mid, "msg": pk, "chat": cpk, "jid": c["jid"], "type": t,
                            "cat": MEDIA_CATS.get(t, "outro"), "date": apple_ts(date), "size": size,
                            "name": os.path.basename(mp), "title": title, "dur": round(dur) if dur else None,
                            "thumb": rel_of(tp) is not None,
                            "key": msg_key(c["jid"], stz, pk), "star": bool(star)})
            return out
        return self.cached(("media", app), load)

    def avatars(self, app):
        """Parte do JID antes do @ -> caminho da foto de perfil mais recente."""
        def load():
            best = {}
            for rel in self._domain_index(WA_DOMAINS[app]):
                if not rel.startswith("Media/Profile/"):
                    continue
                base, _, ext = rel[len("Media/Profile/"):].rpartition(".")
                who, _, ts = base.rpartition("-")
                if ext not in ("jpg", "thumb") or not who or not ts.isdigit():
                    continue
                rank = (int(ts), ext == "jpg")
                if who not in best or rank > best[who][0]:
                    best[who] = (rank, rel)
            return {k: v[1] for k, v in best.items()}
        return self.cached(("avatars", app), load)

    def media_path(self, app, rel):
        if not rel:
            return None
        rel = rel.lstrip("/")
        for cand in (f"Message/{rel}", rel):
            p = self.file(WA_DOMAINS[app], cand)
            if p:
                return p
        return None

    # --- WhatsApp: consultas com a organização aplicada -----------------------
    def wa_chats(self, app):
        chats, starred = self.wa_meta(app)
        st = STATE.app(app)
        valid = {f["id"] for f in STATE.folders(app)}
        favs = {pk: len(s) for pk, s in starred.items()}
        by_jid = {c["jid"]: pk for pk, c in chats.items()}
        for key, val in st["stars"].items():
            pk = by_jid.get(key.split("|", 1)[0])
            if pk is None:
                continue
            base = key in starred.get(pk, ())
            if val != base:
                favs[pk] = favs.get(pk, 0) + (1 if val else -1)
        media = collections.defaultdict(lambda: [0, 0])
        for it in self.wa_media_all(app):
            media[it["chat"]][0] += 1
            media[it["chat"]][1] += it["size"]
        av = self.avatars(app)
        out = []
        for pk, c in chats.items():
            folder = st["chat_folder"].get(c["jid"])
            if folder not in valid:
                folder = auto_folder(c)
            d = st["deleted_chats"].get(c["jid"])
            fav = favs.get(pk, 0)
            out.append(dict(c, folder=folder, manual=c["jid"] in st["chat_folder"], fav=fav,
                            deleted=bool(d), keep=bool(d and d.get("keep_starred") and fav > 0),
                            media_n=media[pk][0], media_bytes=media[pk][1],
                            photo=c["jid"].split("@")[0] in av))
        return {"folders": STATE.folders(app), "chats": out}

    def _sender(self, app, chat, me, fromjid, gname, gfirst, gjid):
        push = self.pushnames(app)
        if me:
            return "Você"
        if gjid or gname:
            return gname or gfirst or push.get(gjid) or jid_label(gjid)
        return chat["name"] if not chat["group"] else (push.get(fromjid) or jid_label(fromjid))

    def _msg_dict(self, app, chat, row, key, fav, deleted):
        (pk, me, date, text, mtype, fromjid, star, stz, mpk, mpath, tpath, title, size, mdur,
         lat, lon, vcard, gname, gfirst, gjid) = row
        media = None
        if mpk:
            full = self.media_path(app, mpath)
            media = {
                "id": mpk, "full": full is not None, "thumb": self.media_path(app, tpath) is not None,
                "name": os.path.basename(mpath) if mpath else None, "title": title,
                "size": full.stat().st_size if full else size, "dur": round(mdur) if mdur else None,
                "lat": lat or None, "lon": lon or None, "vcard": vcard,
            }
        return {"id": pk, "key": key, "me": bool(me), "date": apple_ts(date), "text": text, "type": mtype,
                "sender": self._sender(app, chat, me, fromjid, gname, gfirst, gjid),
                "fav": fav, "deleted": deleted, "media": media}

    def _chat(self, app, chat_pk):
        chat = self.wa_meta(app)[0].get(chat_pk)
        if not chat:
            raise KeyError("Conversa não encontrada")
        return chat

    def wa_messages(self, app, chat_pk, cursor=0, direction="older", limit=150, mode="normal", showdel=False):
        """Paginação sobre a ordem bruta (mais nova primeiro). `cursor` é a posição bruta:
        'older' lê de cursor em diante; 'newer' lê as posições < cursor."""
        chat = self._chat(app, chat_pk)
        vis = Vis(app)
        items = []

        def take(row):
            key = msg_key(chat["jid"], row[7], row[0])
            fav = vis.fav(key, row[6])
            deleted = key in vis.dm
            if (deleted and not showdel) or (not deleted and not vis.chat_ok(chat["jid"], fav, mode)):
                return
            items.append(self._msg_dict(app, chat, row, key, fav, deleted))

        db = self.wa_db(app)
        sql, chunk, done = MSG_SQL.format(o="desc"), 500, False
        if direction == "older":
            pos = cursor
            while len(items) < limit:
                rows = db.execute(sql, (chat_pk, chunk, pos)).fetchall()
                for i, r in enumerate(rows):
                    take(r)
                    if len(items) >= limit:
                        pos += i + 1
                        break
                else:
                    pos += len(rows)
                    if len(rows) < chunk:
                        done = True
                        break
            items.reverse()
        else:
            pos = cursor
            while len(items) < limit and pos > 0:
                start = max(0, pos - chunk)
                rows = db.execute(sql, (chat_pk, pos - start, start)).fetchall()
                for i in range(len(rows) - 1, -1, -1):
                    take(rows[i])
                    if len(items) >= limit:
                        pos = start + i
                        break
                else:
                    pos = start
            done = pos == 0
        db.close()
        return {"items": items, "cursor": pos, "done": done}

    def wa_position(self, app, chat_pk, msg_pk):
        db = self.wa_db(app)
        r = db.execute("select ZMESSAGEDATE from ZWAMESSAGE where Z_PK=? and ZCHATSESSION=?", (msg_pk, chat_pk)).fetchone()
        if not r:
            raise KeyError("Mensagem não encontrada")
        if r[0] is None:
            n = db.execute("""select count(*) from ZWAMESSAGE where ZCHATSESSION=?
                              and (ZMESSAGEDATE is not null or Z_PK > ?)""", (chat_pk, msg_pk)).fetchone()[0]
        else:
            n = db.execute("""select count(*) from ZWAMESSAGE where ZCHATSESSION=?
                              and (ZMESSAGEDATE > ? or (ZMESSAGEDATE = ? and Z_PK > ?))""",
                           (chat_pk, r[0], r[0], msg_pk)).fetchone()[0]
        db.close()
        return n

    def iter_messages(self, app, chat_pk, mode="normal"):
        """Mensagens visíveis em ordem cronológica (para exportação)."""
        chat = self._chat(app, chat_pk)
        vis = Vis(app)
        db = self.wa_db(app)
        for row in db.execute(MSG_SQL.format(o="asc"), (chat_pk, -1, 0)):
            key = msg_key(chat["jid"], row[7], row[0])
            fav = vis.fav(key, row[6])
            if key not in vis.dm and vis.chat_ok(chat["jid"], fav, mode):
                yield self._msg_dict(app, chat, row, key, fav, False)
        db.close()

    def wa_search(self, app, q, chat_pk=None, limit=300):
        nq = norm(q.strip())
        if len(nq) < 2:
            return {"items": [], "more": False}
        chats, _ = self.wa_meta(app)
        vis = Vis(app)
        out, more = [], False
        for pk, cpk, date, text, me, stz, star, ntext in self.wa_text_index(app):
            if nq not in ntext or (chat_pk and cpk != chat_pk):
                continue
            c = chats.get(cpk)
            if not c:
                continue
            key = msg_key(c["jid"], stz, pk)
            mode = "all" if chat_pk and c["jid"] in vis.dc else "normal"
            if key in vis.dm or not vis.chat_ok(c["jid"], vis.fav(key, star), mode):
                continue
            if len(out) >= limit:
                more = True
                break
            n, idx = norm_with_map(text)
            i = n.find(nq)
            a, b = idx[i], idx[min(i + len(nq), len(idx)) - 1] + 1
            s = max(0, a - 60)
            out.append({"msg": pk, "chat": cpk, "chat_name": c["name"], "date": apple_ts(date), "me": bool(me),
                        "pre": ("…" if s else "") + text[s:a], "hit": text[a:b], "post": text[b:b + 100]})
        return {"items": out, "more": more}

    def wa_text_index(self, app):
        """Textos já normalizados (sem acento/minúsculos), do mais novo para o mais antigo."""
        def load():
            db = self.wa_db(app)
            rows = [r + (norm(r[3]),) for r in db.execute(
                "select Z_PK, ZCHATSESSION, ZMESSAGEDATE, ZTEXT, ZISFROMME, ZSTANZAID, ZSTARRED from ZWAMESSAGE "
                "where ZTEXT is not null order by ZMESSAGEDATE desc")]
            db.close()
            return rows
        return self.cached(("text", app), load)

    def wa_media_list(self, app, chat_pk=None, cat="all", sort="size", offset=0, limit=120, mode="normal"):
        chats, _ = self.wa_meta(app)
        vis = Vis(app)
        pool, cats = [], collections.Counter()
        for it in self.wa_media_all(app):
            if chat_pk and it["chat"] != chat_pk:
                continue
            fav = vis.fav(it["key"], it["star"])
            if it["key"] in vis.dm or not vis.chat_ok(it["jid"], fav, mode):
                continue
            cats[it["cat"]] += 1
            if cat == "all" or it["cat"] == cat:
                pool.append(dict(it, fav=fav))
        keys = {"size": (lambda x: x["size"], True), "size_asc": (lambda x: x["size"], False),
                "date": (lambda x: x["date"] or 0, True), "date_asc": (lambda x: x["date"] or 0, False)}
        fn, rev = keys.get(sort, keys["size"])
        pool.sort(key=fn, reverse=rev)
        page = [dict(x, chat_name=chats[x["chat"]]["name"]) for x in pool[offset:offset + limit]]
        return {"total": len(pool), "bytes": sum(x["size"] for x in pool), "cats": cats, "items": page}

    def wa_media(self, app, mpk, thumb=False):
        db = self.wa_db(app)
        row = db.execute("select ZMEDIALOCALPATH, ZTHUMBNAILLOCALPATH from ZWAMEDIAITEM where Z_PK=?", (mpk,)).fetchone()
        db.close()
        if not row:
            return None, None
        rel = row[1] if thumb else row[0]
        return self.media_path(app, rel), rel

    def wa_export_txt(self, app, chat_pk, mode="normal"):
        return "".join(fmt_line(m) + "\n" for m in self.iter_messages(app, chat_pk, mode))


def fmt_line(m):
    when = dt.datetime.fromtimestamp(m["date"]).strftime("%d/%m/%Y %H:%M") if m["date"] else "?"
    body = m["text"] or ""
    if m["media"]:
        md = m["media"]
        att = md["name"] or md["title"] or md["vcard"] or "anexo"
        body = (body + " " if body else "") + f"<anexo: {att}>"
    star = " ★" if m["fav"] else ""
    return f"[{when}]{star} {m['sender']}: {body or TYPE_LABELS.get(m['type'], '<mensagem>')}"


_jpg_cache = collections.OrderedDict()
_jpg_lock = threading.Lock()


def heic_to_jpeg(path):
    with _jpg_lock:
        if path in _jpg_cache:
            _jpg_cache.move_to_end(path)
            return _jpg_cache[path]
    im = Image.open(path)
    exif = im.info.get("exif")
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=90, **({"exif": exif} if exif else {}))
    data = buf.getvalue()
    with _jpg_lock:
        _jpg_cache[path] = data
        while len(_jpg_cache) > 12:
            _jpg_cache.popitem(last=False)
    return data


def find_backups(roots):
    found = {}
    for root in roots:
        root = pathlib.Path(root)
        if not root.is_dir():
            continue
        for d in sorted(root.iterdir()):
            if (d / "Manifest.db").exists() and (d / "Info.plist").exists():
                try:
                    found[d.name] = Backup(d)
                except Exception as e:
                    print(f"Ignorando {d}: {e}", file=sys.stderr)
    return found


# --- Utilidades gerais para qualquer app ---------------------------------------
NOTES_DOM = "AppDomainGroup-group.com.apple.notes"
AB_DB = "Library/AddressBook/AddressBook.sqlitedb"
AB_IMG_DB = "Library/AddressBook/AddressBookImages.sqlitedb"
SYSTEM_DOMAINS = {
    "HomeDomain": "Sistema (dados gerais)", "CameraRollDomain": "Rolo da Câmera", "MediaDomain": "Mídia do sistema",
    "KeychainDomain": "Chaves (Keychain)", "WirelessDomain": "Rede sem fio", "KeyboardDomain": "Teclado",
    "SysContainerDomain": "Contêiner do sistema", "SysSharedContainerDomain": "Contêiner compartilhado do sistema",
    "ManagedPreferencesDomain": "Preferências gerenciadas", "RootDomain": "Raiz do sistema", "DatabaseDomain": "Bancos do sistema",
    "HealthDomain": "Saúde", "InstallDomain": "Instalação", "SystemPreferencesDomain": "Preferências do sistema",
    "TonesDomain": "Toques", "BooksDomain": "Livros", "ProtectedDomain": "Protegido",
}
APPLE_APPS = {
    "com.apple.notes": "Notas", "com.apple.DocumentsApp": "Arquivos", "com.apple.mobilesafari": "Safari",
    "com.apple.PosterBoard": "Tela Bloqueada (papéis de parede)", "com.apple.FileProvider.LocalStorage": "Arquivos (No iPhone)",
    "com.apple.iBooks": "Livros", "com.apple.mobilenotes": "Notas", "com.apple.reminders": "Lembretes",
    "com.apple.mobilecal": "Calendário", "com.apple.Health": "Saúde", "com.apple.weather": "Tempo",
    "com.apple.Maps": "Mapas", "com.apple.mobileslideshow": "Fotos", "com.apple.MobileSMS": "Mensagens",
    "com.apple.VoiceMemos": "Gravador", "com.apple.shortcuts": "Atalhos", "com.apple.news": "News",
    "com.apple.podcasts": "Podcasts", "com.apple.Music": "Música", "com.apple.mobilemail": "Mail",
    "com.apple.AppleMediaDiscovery": "App Store (sugestões)", "com.apple.PegasusConfiguration": "Picture in Picture",
}
_TMP = None


def tmp_dir():
    global _TMP
    if _TMP is None:
        _TMP = tempfile.mkdtemp(prefix="garimpo_")
        atexit.register(shutil.rmtree, _TMP, True)
    return _TMP


def file_sizes(b):
    """fileID -> tamanho. No Windows o scandir já traz o tamanho, então isso é rápido."""
    def load():
        out = {}
        for d in os.scandir(b.path):
            if d.is_dir() and len(d.name) == 2:
                for f in os.scandir(d.path):
                    out[f.name] = f.stat().st_size
        return out
    return b.cached("sizes", load)


def open_sqlite(b, domain, rel):
    """Abre um banco do backup só para leitura. Se houver um -wal com dados, aplica-o numa cópia temporária
    (o original nunca é tocado)."""
    path = b.file(domain, rel)
    if not path:
        raise KeyError("Arquivo não encontrado")
    wal = b.file(domain, rel + "-wal")
    if not wal or wal.stat().st_size == 0:
        return connect_ro(path)

    def copy():
        d = pathlib.Path(tempfile.mkdtemp(dir=tmp_dir()))
        shutil.copyfile(path, d / "db.sqlite")
        shutil.copyfile(wal, d / "db.sqlite-wal")
        c = sqlite3.connect(d / "db.sqlite")
        c.execute("pragma wal_checkpoint(truncate)")
        c.execute("pragma journal_mode=delete")
        c.close()
        return d / "db.sqlite"
    return connect_ro(b.cached(("walcopy", domain, rel), copy))


def app_catalog(b):
    """bundle id -> {nome, ícone}, a partir do Info.plist do backup."""
    def load():
        out = {}
        for bid, meta in (b.info.get("Applications") or {}).items():
            name = None
            if meta.get("iTunesMetadata"):
                try:
                    name = plistlib.loads(meta["iTunesMetadata"]).get("itemName")
                except Exception:
                    pass
            out[bid] = {"name": name or bid, "icon": meta.get("PlaceholderIcon")}
        return out
    return b.cached("apps", load)


def domain_owner(dom, apps):
    """(bundle id do app dono do domínio ou None, tipo)."""
    for prefix, kind in (("AppDomainGroup-", "grupo"), ("AppDomainPlugin-", "extensão"), ("AppDomain-", "app")):
        if dom.startswith(prefix):
            part = dom[len(prefix):]
            if part.startswith("group."):
                part = part[6:]
            if part in apps:
                return part, kind
            best = max((bid for bid in apps if part.startswith(bid + ".") or bid.startswith(part + ".")), key=len, default=None)
            return best or part, kind
    return None, "sistema"


def explorer_entries(b):
    def load():
        apps = app_catalog(b)
        sizes = file_sizes(b)
        db = connect_ro(b.path / "Manifest.db")
        stats = collections.defaultdict(lambda: [0, 0])
        for dom, fid in db.execute("select domain, fileID from Files where flags=1"):
            s = stats[dom]
            s[0] += 1
            s[1] += sizes.get(fid, 0)
        db.close()
        entries = {}
        for dom, (n, size) in stats.items():
            bid, kind = domain_owner(dom, apps)
            key = bid or dom
            e = entries.setdefault(key, {"key": key, "bid": bid, "domains": [], "files": 0, "bytes": 0,
                                         "name": apps.get(bid, {}).get("name") if bid else None,
                                         "icon": bool(bid and apps.get(bid, {}).get("icon")),
                                         "kind": "app" if bid else "sistema"})
            if not e["name"]:
                e["name"] = (APPLE_APPS.get(bid) or next((v for k, v in APPLE_APPS.items() if bid.startswith(k + ".")), bid)) \
                    if bid else SYSTEM_DOMAINS.get(dom.split("-")[0], dom)
            e["domains"].append({"domain": dom, "kind": kind, "files": n, "bytes": size})
            e["files"] += n
            e["bytes"] += size
        out = sorted(entries.values(), key=lambda e: -e["bytes"])
        for e in out:
            e["domains"].sort(key=lambda d: -d["bytes"])
        return out
    return b.cached("explorer", load)


def explorer_ls(b, domain, d):
    idx = b._domain_index(domain)
    sizes = file_sizes(b)
    prefix = d.strip("/") + "/" if d.strip("/") else ""
    dirs, files = {}, []
    for rel, fid in idx.items():
        if not rel.startswith(prefix) or rel == prefix:
            continue
        rest = rel[len(prefix):]
        if "/" in rest:
            e = dirs.setdefault(rest.split("/", 1)[0], [0, 0])
            e[0] += 1
            e[1] += sizes.get(fid, 0)
        else:
            files.append({"name": rest, "size": sizes.get(fid, 0)})
    return {"dirs": [{"name": k, "files": v[0], "bytes": v[1]} for k, v in sorted(dirs.items(), key=lambda x: x[0].lower())],
            "files": sorted(files, key=lambda f: f["name"].lower())}


def sniff(head, name=""):
    ext = os.path.splitext(name)[1].lower()
    if head.startswith(b"SQLite format 3\x00"):
        return "sqlite"
    if head.startswith(b"bplist0") or (head.lstrip().startswith(b"<?xml") and b"plist" in head[:300]) or ext == ".plist":
        return "plist"
    if head.startswith(b"\x1f\x8b"):
        return "gzip"
    if head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc", b"ftypheif"):
        return "heic"
    if head.startswith((b"\xff\xd8\xff", b"\x89PNG", b"GIF8")) or (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
        return "image"
    if head[4:8] == b"ftyp":
        return "audio" if ext in (".m4a", ".aac") else "video"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith((b"OggS", b"ID3", b"caff", b"fLaC")) or ext in (".mp3", ".opus", ".caf", ".wav", ".m4a"):
        return "audio"
    try:
        head.decode("utf-8")
        return "json" if head.lstrip()[:1] in (b"{", b"[") else "text"
    except UnicodeDecodeError:
        # pode ter cortado um caractere multibyte no fim da amostra
        try:
            head[:-3].decode("utf-8")
            return "text"
        except UnicodeDecodeError:
            return "binary"


def keyed_unarchive(pl):
    """Torna legível um plist NSKeyedArchiver (o formato que muitos apps usam)."""
    objs = pl.get("$objects", [])

    def res(v, depth=0):
        if depth > 40:
            return "…"
        if isinstance(v, plistlib.UID):
            v = objs[v.data] if v.data < len(objs) else None
            if v == "$null":
                return None
        if isinstance(v, dict):
            cls = v.get("$class")
            cname = objs[cls.data].get("$classname") if isinstance(cls, plistlib.UID) and isinstance(objs[cls.data], dict) else None
            if "NS.keys" in v and "NS.objects" in v:
                return {str(res(k, depth + 1)): res(o, depth + 1) for k, o in zip(v["NS.keys"], v["NS.objects"])}
            if "NS.objects" in v:
                return [res(o, depth + 1) for o in v["NS.objects"]]
            if "NS.string" in v:
                return v["NS.string"]
            if "NS.time" in v:
                return dt.datetime.fromtimestamp(v["NS.time"] + APPLE_EPOCH).isoformat()
            if "NS.data" in v:
                return res(v["NS.data"], depth + 1)
            out = {k: res(x, depth + 1) for k, x in v.items() if k != "$class"}
            if cname:
                out = {"$classe": cname, **out}
            return out
        if isinstance(v, list):
            return [res(x, depth + 1) for x in v]
        return v
    return res(pl.get("$top", {}).get("root", pl.get("$top")))


def to_jsonable(v, depth=0):
    if depth > 60:
        return "…"
    if isinstance(v, dict):
        if v.get("$archiver") == "NSKeyedArchiver" and depth == 0:
            return to_jsonable(keyed_unarchive(v), depth + 1)
        return {str(k): to_jsonable(x, depth + 1) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [to_jsonable(x, depth + 1) for x in v]
    if isinstance(v, bytes):
        if v.startswith(b"bplist0"):
            try:
                return {"$plist_embutido": to_jsonable(plistlib.loads(v))}
            except Exception:
                pass
        try:
            s = v.decode("utf-8")
            if s.isprintable():
                return s
        except UnicodeDecodeError:
            pass
        return f"<{len(v)} bytes> {v[:48].hex(' ')}{' …' if len(v) > 48 else ''}"
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    if isinstance(v, plistlib.UID):
        return {"UID": v.data}
    return v


def decode_blob(data):
    """Tenta interpretar um blob: gzip, plist, texto ou imagem."""
    if data.startswith(b"\x1f\x8b"):
        try:
            data = gzip.decompress(data)
        except Exception:
            pass
    kind = sniff(data[:4096])
    if kind == "plist":
        try:
            return {"kind": "plist", "json": to_jsonable(plistlib.loads(data))}
        except Exception:
            pass
    if kind in ("text", "json"):
        return {"kind": "text", "text": data[:200_000].decode("utf-8", "replace")}
    if kind in ("image", "heic"):
        return {"kind": "image"}
    return {"kind": "binary", "size": len(data), "hex": hexdump(data[:4096])}


def hexdump(data):
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        lines.append(f"{i:08x}  {chunk.hex(' '):<47}  {''.join(chr(c) if 32 <= c < 127 else '.' for c in chunk)}")
    return "\n".join(lines)


def explorer_info(b, domain, rel):
    path = b.file(domain, rel)
    if not path:
        raise KeyError("Arquivo não encontrado")
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(4096)
    kind = sniff(head, rel)
    out = {"name": os.path.basename(rel), "path": rel, "size": size, "type": kind}
    if kind == "plist":
        try:
            out["json"] = to_jsonable(plistlib.loads(path.read_bytes()))
        except Exception as e:
            out.update(type="binary", hex=hexdump(head[:2048]), note=f"plist inválido: {e}")
    elif kind in ("text", "json"):
        with open(path, "rb") as f:
            out["text"] = f.read(300_000).decode("utf-8", "replace")
        out["truncated"] = size > 300_000
    elif kind == "gzip":
        out.update(decode_blob(path.read_bytes() if size < 50_000_000 else head))
        out["type"] = "gzip:" + out.pop("kind")
    elif kind == "binary":
        out["hex"] = hexdump(head[:2048])
    elif kind == "sqlite":
        db = open_sqlite(b, domain, rel)
        tables = []
        for (name,) in db.execute("select name from sqlite_master where type='table' order by name"):
            try:
                n = db.execute(f'select count(*) from "{name}"').fetchone()[0]
                cols = [r[1] for r in db.execute(f'pragma table_info("{name}")')]
            except sqlite3.Error:
                n, cols = None, []  # ex.: tabela de busca com tokenizer próprio da Apple
            tables.append({"name": name, "rows": n, "cols": cols})
        db.close()
        out["tables"] = tables
    return out


def sql_cell(v):
    if isinstance(v, bytes):
        return {"blob": len(v), "kind": sniff(v[:64]) if not v.startswith(b"\x1f\x8b") else "gzip"}
    if isinstance(v, str) and len(v) > 400:
        return {"text": v[:400], "len": len(v)}
    return v


def _ro_authorizer(action, *args):
    allowed = (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
               getattr(sqlite3, "SQLITE_RECURSIVE", 33))
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def explorer_rows(b, domain, rel, table=None, offset=0, limit=100, sql=None):
    db = open_sqlite(b, domain, rel)
    deadline = time.time() + 8
    db.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 10000)
    try:
        if sql:
            db.set_authorizer(_ro_authorizer)  # só SELECT
            cur = db.execute(sql)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(500)
            return {"cols": cols, "rows": [[sql_cell(v) for v in r] for r in rows], "more": bool(cur.fetchone())}
        tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
        if table not in tables:
            raise KeyError("Tabela não encontrada")
        try:
            cur = db.execute(f'select rowid as "__rowid__", * from "{table}" limit ? offset ?', (limit, offset))
        except sqlite3.OperationalError:
            cur = db.execute(f'select * from "{table}" limit ? offset ?', (limit, offset))
        cols = [d[0] for d in cur.description]
        return {"cols": cols, "rows": [[sql_cell(v) for v in r] for r in cur.fetchall()]}
    except sqlite3.DatabaseError as e:
        if "not authorized" in str(e):
            raise ValueError("Só consultas de leitura (SELECT) são permitidas — o backup nunca é alterado.")
        raise ValueError(f"Erro na consulta: {e}")
    finally:
        db.close()


def explorer_blob(b, domain, rel, table, rowid, col):
    db = open_sqlite(b, domain, rel)
    try:
        if table not in {r[0] for r in db.execute("select name from sqlite_master where type='table'")}:
            raise KeyError("Tabela não encontrada")
        cols = [r[1] for r in db.execute(f'pragma table_info("{table}")')]
        if col not in cols:
            raise KeyError("Coluna não encontrada")
        r = db.execute(f'select "{col}" from "{table}" where rowid=?', (rowid,)).fetchone()
    finally:
        db.close()
    if not r or not isinstance(r[0], bytes):
        raise KeyError("Blob não encontrado")
    return r[0]


# --- Contatos ----------------------------------------------------------------------
AB_LABELS = {"Mobile": "Celular", "Home": "Casa", "Work": "Trabalho", "Main": "Principal", "Other": "Outro",
             "iPhone": "iPhone", "HomeFAX": "Fax (casa)", "WorkFAX": "Fax (trabalho)", "Pager": "Pager",
             "HomePage": "Página", "Anniversary": "Aniversário de casamento", "School": "Escola"}
AB_PROPS = {3: "phones", 4: "emails", 5: "addresses", 22: "urls", 12: "dates", 23: "related", 13: "ims", 46: "social"}


def ab_label(raw):
    if not raw:
        return ""
    m = re.match(r"_\$!<(.+)>!\$_", raw)
    return AB_LABELS.get(m.group(1), m.group(1)) if m else raw


def apple_date_str(v):
    try:
        d = dt.datetime(2001, 1, 1) + dt.timedelta(seconds=float(v))
    except (TypeError, ValueError, OverflowError):
        return None
    return d.strftime("%d/%m") if d.year <= 1604 else d.strftime("%d/%m/%Y")


def contacts(b):
    def load():
        db = open_sqlite(b, "HomeDomain", AB_DB)
        tables = {r[0] for r in db.execute("select name from sqlite_master where type='table'")}
        cols = _cols(db, "ABPerson")
        want = ["ROWID", "First", "Middle", "Last", "Prefix", "Suffix", "Nickname", "Organization", "Department",
                "JobTitle", "Note", "Birthday", "CreationDate", "ModificationDate"]
        sel = ", ".join(c if c in cols else "NULL" for c in want)
        people = {}
        for r in db.execute(f"select {sel} from ABPerson"):
            p = dict(zip(want, r))
            name = " ".join(x for x in (p["Prefix"], p["First"], p["Middle"], p["Last"], p["Suffix"]) if x)
            people[p["ROWID"]] = {
                "id": p["ROWID"], "name": name, "org": p["Organization"], "dept": p["Department"],
                "job": p["JobTitle"], "nick": p["Nickname"], "note": p["Note"],
                "birthday": apple_date_str(p["Birthday"]) if p["Birthday"] not in (None, "") else None,
                "created": apple_ts(p["CreationDate"]), "modified": apple_ts(p["ModificationDate"]),
                "phones": [], "emails": [], "addresses": [], "urls": [], "dates": [], "related": [], "ims": [], "social": [],
            }
        labels = dict(db.execute("select ROWID, value from ABMultiValueLabel")) if "ABMultiValueLabel" in tables else {}
        entries = collections.defaultdict(dict)
        if {"ABMultiValueEntry", "ABMultiValueEntryKey"} <= tables:
            keys = dict(db.execute("select ROWID, value from ABMultiValueEntryKey"))
            for parent, key, value in db.execute("select parent_id, key, value from ABMultiValueEntry"):
                entries[parent][keys.get(key, key)] = value
        for uid, rid, prop, label, value in db.execute("select UID, record_id, property, label, value from ABMultiValue"):
            p, field = people.get(rid), AB_PROPS.get(prop)
            if not p or not field:
                continue
            if field == "dates":
                value = apple_date_str(value)
            elif field in ("addresses", "ims", "social") and uid in entries:
                e = entries[uid]
                value = ", ".join(str(v) for k, v in e.items() if v and k not in ("CountryCode",)) if field == "addresses" \
                    else (e.get("username") or e.get("url") or ", ".join(map(str, e.values())))
            if value:
                p[field].append({"label": ab_label(labels.get(label, "")), "value": str(value)})
        db.close()
        photos = set()
        if b.has("HomeDomain", AB_IMG_DB):
            idb = open_sqlite(b, "HomeDomain", AB_IMG_DB)
            for t in ("ABThumbnailImage", "ABFullSizeImage"):
                try:
                    photos |= {r[0] for r in idb.execute(f"select record_id from {t}")}
                except sqlite3.Error:
                    pass
            idb.close()
        out = []
        for p in people.values():
            if not p["name"]:
                p["name"] = p["org"] or p["nick"] or (p["phones"] or p["emails"] or [{"value": "Sem nome"}])[0]["value"]
            p["photo"] = p["id"] in photos
            out.append(p)
        out.sort(key=lambda p: norm(p["name"]))
        return out
    return b.cached("contacts", load)


def contact_photo(b, rid, full=True):
    idb = open_sqlite(b, "HomeDomain", AB_IMG_DB)
    try:
        for t in (("ABFullSizeImage", "ABThumbnailImage") if full else ("ABThumbnailImage", "ABFullSizeImage")):
            try:
                r = idb.execute(f"select data from {t} where record_id=? order by length(data) desc limit 1", (rid,)).fetchone()
            except sqlite3.Error:
                continue
            if r and r[0]:
                data = r[0]
                for magic in (b"\xff\xd8\xff", b"\x89PNG"):
                    i = data.find(magic)
                    if i >= 0:
                        return data[i:]
                return data
    finally:
        idb.close()
    return None


def vcard(b, people):
    def e(s):
        return str(s).replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")
    out = []
    for p in people:
        lines = ["BEGIN:VCARD", "VERSION:3.0", f"FN:{e(p['name'])}", f"N:;{e(p['name'])};;;"]
        if p["org"]:
            lines.append(f"ORG:{e(p['org'])}")
        if p["job"]:
            lines.append(f"TITLE:{e(p['job'])}")
        for ph in p["phones"]:
            lines.append(f"TEL;TYPE={e(ph['label'] or 'CELL')}:{e(ph['value'])}")
        for em in p["emails"]:
            lines.append(f"EMAIL;TYPE=INTERNET:{e(em['value'])}")
        for u in p["urls"]:
            lines.append(f"URL:{e(u['value'])}")
        for a in p["addresses"]:
            lines.append(f"ADR;TYPE={e(a['label'] or 'HOME')}:;;{e(a['value'])};;;;")
        if p["birthday"] and len(p["birthday"]) == 10:
            d, m, y = p["birthday"].split("/")
            lines.append(f"BDAY:{y}-{m}-{d}")
        if p["note"]:
            lines.append(f"NOTE:{e(p['note'])}")
        if p["photo"]:
            img = contact_photo(b, p["id"], full=False)
            if img:
                lines.append("PHOTO;ENCODING=b;TYPE=" + ("PNG" if img.startswith(b"\x89PNG") else "JPEG") + ":"
                             + base64.b64encode(img).decode())
        lines.append("END:VCARD")
        out.append("\r\n".join(lines))
    return "\r\n".join(out) + "\r\n"


# --- Notas -----------------------------------------------------------------------
FOLDER_NAMES = {"Recently Deleted": "Apagadas Recentemente", "Notes": "Notas", "Quick Notes": "Notas Rápidas"}
IMAGE_UTIS = {"public.jpeg", "public.png", "public.heic", "public.heif", "public.gif", "public.tiff", "public.image",
              "com.compuserve.gif"}


def pb_parse(b):
    """Decodificador mínimo de protobuf: campo -> lista de valores (int ou bytes)."""
    out, i, n = collections.defaultdict(list), 0, len(b)
    while i < n:
        k = s = 0
        while True:
            x = b[i]; i += 1; k |= (x & 0x7f) << s; s += 7
            if x < 0x80:
                break
        f, t = k >> 3, k & 7
        if t == 0:
            v = s = 0
            while True:
                x = b[i]; i += 1; v |= (x & 0x7f) << s; s += 7
                if x < 0x80:
                    break
        elif t == 2:
            ln = s = 0
            while True:
                x = b[i]; i += 1; ln |= (x & 0x7f) << s; s += 7
                if x < 0x80:
                    break
            v = b[i:i + ln]; i += ln
        elif t == 5:
            v = b[i:i + 4]; i += 4
        elif t == 1:
            v = b[i:i + 8]; i += 8
        else:
            raise ValueError("protobuf inválido")
        out[f].append(v)
    return out


def pb1(m, f, default=None):
    v = m.get(f)
    return v[0] if v else default


def note_proto(data):
    if not data:
        return None
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    elif data[:1] == b"x" and data[1:2] in (b"\x01", b"\x5e", b"\x9c", b"\xda"):  # zlib (notas antigas)
        data = zlib.decompress(data)
    doc = pb_parse(pb1(pb_parse(pb1(pb_parse(data), 2, b"")), 3, b""))
    text16 = pb1(doc, 2, b"").decode("utf-8", "replace").encode("utf-16-le")
    runs = []
    for rb in doc.get(5, []):
        r = pb_parse(rb)
        para = pb_parse(pb1(r, 2, b""))
        chk = pb_parse(pb1(para, 5, b"")) if para.get(5) else None
        att = pb_parse(pb1(r, 12, b"")) if r.get(12) else None
        uti = None
        if att and att.get(2) and isinstance(att[2][0], bytes):
            try:
                uti = att[2][0].decode("utf-8")
                if not re.fullmatch(r"[\w.\-]+", uti):
                    uti = None
            except UnicodeDecodeError:
                pass
        runs.append({
            "len": pb1(r, 1, 0), "style": pb1(para, 1, -1) if para else -1, "indent": pb1(para, 4, 0) if para else 0,
            "done": bool(pb1(chk, 2, 0)) if chk is not None else None,
            "weight": pb1(r, 5, 0), "under": bool(pb1(r, 6, 0)), "strike": bool(pb1(r, 7, 0)),
            "link": pb1(r, 9, b"").decode("utf-8", "replace") if r.get(9) else None,
            "att": pb1(att, 1, b"").decode("utf-8", "replace") if att else None, "uti": uti,
        })
    segs, pos = [], 0
    for r in runs:
        segs.append((text16[pos * 2:(pos + r["len"]) * 2].decode("utf-16-le", "replace"), r))
        pos += r["len"]
    if pos * 2 < len(text16):
        segs.append((text16[pos * 2:].decode("utf-16-le", "replace"), {"style": -1}))
    return segs


def note_plain(segs):
    return "".join(t for t, _ in segs).replace("￼", "") if segs else ""


def notes_index(b):
    """Pastas, notas (com texto puro para busca) e anexos do app Notas."""
    def load():
        db = open_sqlite(b, NOTES_DOM, "NoteStore.sqlite")
        ents = dict(db.execute("select Z_NAME, Z_ENT from Z_PRIMARYKEY"))
        cols = _cols(db, "ZICCLOUDSYNCINGOBJECT")
        pick = lambda *cs: next((c for c in cs if c in cols), "NULL")
        created, modified = pick("ZCREATIONDATE3", "ZCREATIONDATE1", "ZCREATIONDATE"), pick("ZMODIFICATIONDATE1", "ZMODIFICATIONDATE")
        ftitle = pick("ZTITLE2", "ZTITLE")
        folders = {}
        for pk, title, ftype, parent in db.execute(
                f"select Z_PK, {ftitle}, {pick('ZFOLDERTYPE')}, {pick('ZPARENT')} from ZICCLOUDSYNCINGOBJECT where Z_ENT=?",
                (ents.get("ICFolder"),)):
            folders[pk] = {"id": pk, "name": FOLDER_NAMES.get(title, title or "Sem nome"), "type": ftype or 0,
                           "parent": parent, "count": 0}
        atts = collections.Counter(r[0] for r in db.execute(
            "select ZNOTE from ZICCLOUDSYNCINGOBJECT where Z_ENT=? and ZNOTE is not null", (ents.get("ICAttachment"),)))
        notes = {}
        for pk, title, snippet, folder, c, m, locked, deleted, data in db.execute(f"""
                select n.Z_PK, n.{pick('ZTITLE1', 'ZTITLE')}, n.{pick('ZSNIPPET')}, n.{pick('ZFOLDER')}, n.{created},
                       n.{modified}, n.{pick('ZISPASSWORDPROTECTED')}, n.{pick('ZMARKEDFORDELETION')}, d.ZDATA
                from ZICCLOUDSYNCINGOBJECT n left join ZICNOTEDATA d on d.ZNOTE = n.Z_PK
                where n.Z_ENT = ?""", (ents.get("ICNote"),)):
            if deleted and not data:
                continue  # excluída e sem conteúdo
            try:
                text = "" if locked else note_plain(note_proto(data))
            except Exception:
                text = ""
            notes[pk] = {"id": pk, "title": title or (text.strip().split("\n")[0][:80] if text else "Sem título"),
                         "snippet": snippet or text.strip()[len(title or ""):][:120].strip(), "folder": folder,
                         "created": apple_ts(c), "modified": apple_ts(m), "locked": bool(locked),
                         "deleted": bool(deleted), "atts": atts.get(pk, 0), "_text": text}
            if folder in folders and not deleted:
                folders[folder]["count"] += 1
        db.close()
        return {"folders": folders, "notes": notes, "ents": ents}
    return b.cached("notes", load)


def notes_files(b):
    """Mapas de arquivos do app Notas: mídia por identificador e prévias por identificador do anexo."""
    def load():
        media, previews = collections.defaultdict(list), collections.defaultdict(list)
        for rel in b._domain_index(NOTES_DOM):
            parts = rel.split("/")
            if len(parts) >= 4 and parts[0] == "Accounts":
                if parts[2] in ("Media", "ExportableMedia") and len(parts) >= 5:
                    media[parts[3]].append(rel)
                elif parts[2] in ("Previews", "FallbackImages"):
                    previews[parts[3][:36]].append(rel)
                elif parts[2] == "Thumbnails" and len(parts) >= 5:
                    previews[parts[3]].append(rel)
        return media, previews
    return b.cached("notes_files", load)


def best_preview(rels):
    def score(r):
        m = re.search(r"-(\d+)x(\d+)-", r)
        area = int(m.group(1)) * int(m.group(2)) if m else 10 ** 9 if "FallbackImages" in r else 0
        return (area, "oriented" in r, "Light" in r)
    imgs = [r for r in rels if r.lower().endswith((".jpg", ".jpeg", ".png", ".heic"))]
    return max(imgs, key=score) if imgs else None


def note_attachments(b, note_pk):
    db = open_sqlite(b, NOTES_DOM, "NoteStore.sqlite")
    ents = notes_index(b)["ents"]
    cols = _cols(db, "ZICCLOUDSYNCINGOBJECT")
    opt = lambda c: f"a.{c}" if c in cols else "NULL"
    rows = db.execute(f"""
        select a.Z_PK, a.ZIDENTIFIER, a.ZTYPEUTI, {opt('ZPARENTATTACHMENT')}, {opt('ZTITLE')}, {opt('ZURLSTRING')},
               {opt('ZALTTEXT')}, m.ZIDENTIFIER, m.ZFILENAME
        from ZICCLOUDSYNCINGOBJECT a left join ZICCLOUDSYNCINGOBJECT m on m.Z_PK = a.ZMEDIA
        where a.Z_ENT in (?, ?) and (a.ZNOTE = ? or {opt('ZNOTE1')} = ?)""",
        (ents.get("ICAttachment"), ents.get("ICInlineAttachment", -1), note_pk, note_pk)).fetchall()
    db.close()
    media, previews = notes_files(b)
    out = {}
    for pk, ident, uti, parent, title, url, alt, mid, fname in rows:
        files = media.get(mid, []) if mid else []
        file = next((f for f in files if fname and f.endswith("/" + fname)), files[0] if files else None)
        out[ident] = {"pk": pk, "uti": uti or "", "parent": parent, "title": title, "url": url, "alt": alt,
                      "file": file, "name": fname, "preview": best_preview(previews.get(ident, []))}
    by_pk = {a["pk"]: a for a in out.values()}
    for a in out.values():
        if a["parent"] in by_pk:
            by_pk[a["parent"]].setdefault("children", []).append(a)
    return out


def note_html(b, base, note_pk, data):
    """Converte o conteúdo da nota em HTML seguro (texto escapado)."""
    segs = note_proto(data)
    atts = note_attachments(b, note_pk)
    raw = lambda rel, jpg=False: f"{base}/raw?domain={urllib.parse.quote(NOTES_DOM)}&path={urllib.parse.quote(rel)}" + ("&jpg=1" if jpg else "")

    def img(rel):
        heic = rel.lower().endswith((".heic", ".heif"))
        return f'<img class="nimg" loading="lazy" src="{raw(rel, heic and HEIC_OK)}" data-full="{raw(rel, heic and HEIC_OK)}">'

    def attachment(ident, uti):
        a = atts.get(ident)
        uti = (a or {}).get("uti") or uti or ""
        if not a:
            return '<span class="natt">📎 anexo</span>'
        if uti.startswith("com.apple.notes.inlinetextattachment"):
            return f'<span class="ntag">{html.escape(a["alt"] or "")}</span>'
        if uti == "com.apple.notes.gallery":
            kids = a.get("children", [])
            return '<div class="ngallery">' + "".join(img(k["file"]) if k["file"] else (img(k["preview"]) if k["preview"] else "") for k in kids) + "</div>"
        if (uti in IMAGE_UTIS or uti.startswith("public.") and "image" in uti) and a["file"]:
            return img(a["file"])
        if uti == "public.url":
            u = a["url"] or ""
            safe = u if u.startswith(("http://", "https://")) else "#"
            return f'<a class="ncard" href="{html.escape(safe)}" target="_blank" rel="noopener">🔗 {html.escape(a["title"] or u)}</a>'
        if uti == "com.apple.notes.table":
            return '<div class="ncard">📊 Tabela <small>(a visualização de tabelas ainda não é suportada)</small></div>'
        if a["preview"]:
            label = {"com.apple.drawing": "Desenho", "com.apple.drawing.2": "Desenho", "com.apple.paper": "Rascunho/esboço"}.get(uti, "")
            return img(a["preview"]) + (f'<div class="ncap">{label}</div>' if label else "")
        if a["file"]:
            nm = html.escape(a["name"] or os.path.basename(a["file"]))
            return f'<a class="ncard" href="{raw(a["file"])}&amp;dl=1">📄 {nm}</a>'
        return f'<span class="natt">📎 {html.escape(uti)}</span>'

    def inline(text, r):
        out = []
        for ch in re.split("(￼)", text):
            if ch == "￼":
                out.append(attachment(r.get("att"), r.get("uti")) if r.get("att") else "")
            elif ch:
                out.append(html.escape(ch))
        s = "".join(out)
        if not s:
            return s
        w = r.get("weight", 0)
        if w in (1, 3):
            s = f"<b>{s}</b>"
        if w in (2, 3):
            s = f"<i>{s}</i>"
        if r.get("under"):
            s = f"<u>{s}</u>"
        if r.get("strike"):
            s = f"<s>{s}</s>"
        link = r.get("link")
        if link and link.startswith(("http://", "https://", "mailto:", "tel:")):
            s = f'<a href="{html.escape(link)}" target="_blank" rel="noopener">{s}</a>'
        return s

    # quebra em parágrafos; cada parágrafo herda o estilo do primeiro trecho
    paras, cur, cur_style = [], [], None
    for text, r in segs:
        pieces = text.split("\n")
        for k, piece in enumerate(pieces):
            if piece:
                cur.append(inline(piece, r))
                if cur_style is None:
                    cur_style = r
            if k < len(pieces) - 1:
                paras.append(("".join(cur), cur_style or r))
                cur, cur_style = [], None
    if cur:
        paras.append(("".join(cur), cur_style or {}))

    out, open_list = [], None
    tags = {100: "ul", 101: "ul class=\"dash\"", 102: "ol"}
    for content, r in paras:
        st = r.get("style", -1)
        lt = tags.get(st)
        if lt != open_list:
            if open_list:
                out.append(f"</{open_list.split()[0]}>")
            if lt:
                out.append(f"<{lt}>")
            open_list = lt
        indent = f' style="margin-left:{r.get("indent", 0) * 24}px"' if r.get("indent") else ""
        if lt:
            out.append(f"<li{indent}>{content}</li>")
        elif st == 0:
            out.append(f"<h1>{content}</h1>")
        elif st == 1:
            out.append(f"<h2>{content}</h2>")
        elif st == 2:
            out.append(f"<h3>{content}</h3>")
        elif st == 4:
            out.append(f"<pre{indent}>{content}</pre>")
        elif st == 103:
            out.append(f'<div class="nchk{" done" if r.get("done") else ""}"{indent}>{"☑" if r.get("done") else "☐"} {content}</div>')
        else:
            out.append(f"<p{indent}>{content or '&nbsp;'}</p>")
    if open_list:
        out.append(f"</{open_list.split()[0]}>")
    return "".join(out)


def note_detail(b, base, pk):
    idx = notes_index(b)
    n = idx["notes"].get(pk)
    if not n:
        raise KeyError("Nota não encontrada")
    out = {k: v for k, v in n.items() if not k.startswith("_")}
    f = idx["folders"].get(n["folder"])
    out["folder_name"] = f["name"] if f else None
    if n["locked"]:
        out["html"] = '<div class="ncard">🔒 Nota protegida por senha — o conteúdo é criptografado e não pode ser lido.</div>'
        return out
    db = open_sqlite(b, NOTES_DOM, "NoteStore.sqlite")
    r = db.execute("select ZDATA from ZICNOTEDATA where ZNOTE=?", (pk,)).fetchone()
    db.close()
    try:
        out["html"] = note_html(b, base, pk, r[0]) if r and r[0] else "<p><i>Nota vazia</i></p>"
    except Exception as e:
        out["html"] = f'<div class="ncard">Não consegui decodificar esta nota ({html.escape(str(e))}).</div>' \
                      f'<pre>{html.escape(n["_text"])}</pre>'
    return out


def _cols(db, table):
    return {r[1] for r in db.execute(f"pragma table_info({table})")}


# --- Servidor HTTP -----------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    backups = {}

    allowed_hosts = set()  # preenchido em main(): só o próprio endereço do app
    # A página só pode falar com o próprio servidor; nada de scripts, imagens ou conexões externas.
    CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
           "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; frame-src 'self'; "
           "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

    def log_message(self, *a):
        pass

    def host_ok(self):
        """Recusa pedidos cujo Host não seja o do app (proteção contra DNS rebinding: um site na internet
        apontando o próprio nome para 127.0.0.1 para tentar ler seus dados)."""
        return (self.headers.get("Host") or "").lower() in self.allowed_hosts

    def security_headers(self, ctype=""):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if ctype.startswith("text/html"):
            self.send_header("Content-Security-Policy", self.CSP)

    def send_bytes(self, data, ctype, status=200, download=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.security_headers(ctype)
        if download:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(download))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                        "application/json; charset=utf-8", status)

    def send_text(self, text, ctype="text/plain; charset=utf-8", status=200, download=None):
        self.send_bytes(text.encode("utf-8"), ctype, status, download)

    def send_file(self, path, name, download=False):
        if not path:
            return self.send_text("Arquivo não está no backup", status=404)
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m and size:
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype_for(name))
        self.security_headers()
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "max-age=86400")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def handle_errors(self, fn):
        try:
            fn()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass
        except KeyError as e:
            self.send_json({"error": str(e).strip("'\"")}, 404)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    last_seen = time.time()

    def do_GET(self):
        if not self.host_ok():
            return self.send_bytes(b'{"error": "Host nao permitido"}', "application/json", 403)
        Handler.last_seen = time.time()
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        parts = [urllib.parse.unquote(p) for p in url.path.strip("/").split("/") if p]
        self.handle_errors(lambda: self.route(parts, q))

    def do_POST(self):
        if not self.host_ok():
            return self.send_bytes(b'{"error": "Host nao permitido"}', "application/json", 403)

        def run():
            # bloqueia requisições vindas de outros sites abertos no navegador
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlparse(origin).netloc != self.headers.get("Host"):
                return self.send_json({"error": "Origem não permitida"}, 403)
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self.send_json({"error": "Use JSON"}, 415)
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            parts = [p for p in urllib.parse.urlparse(self.path).path.strip("/").split("/") if p]
            if parts == ["api", "quit"]:
                self.send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if len(parts) == 4 and parts[:2] == ["api", "wa"] and parts[2] in WA_DOMAINS and parts[3] == "action":
                return self.send_json(STATE.apply(parts[2], body))
            raise KeyError("Rota não encontrada")
        self.handle_errors(run)

    def route(self, parts, q):
        if not parts:
            return self.send_text(INDEX_HTML, "text/html; charset=utf-8")
        if parts[0] != "api":
            return self.send_text("Não encontrado", status=404)
        if parts[1:] == ["ping"]:
            return self.send_json({"app": "garimpo"})
        if parts[1:] == ["backups"]:
            return self.send_json([b.summary() for b in self.backups.values()])
        if len(parts) < 4 or parts[1] != "b" or parts[2] not in self.backups:
            raise KeyError("Backup não encontrado")
        b, rest = self.backups[parts[2]], parts[3:]
        if b.encrypted:
            raise KeyError("Backup criptografado: ainda não suportado")


        # explorador genérico de apps
        dom, rel = q.get("domain", ""), q.get("path", "")
        if rest == ["explorer"]:
            return self.send_json(explorer_entries(b))
        if rest == ["explorer", "ls"]:
            return self.send_json(explorer_ls(b, dom, q.get("dir", "")))
        if rest == ["explorer", "info"]:
            return self.send_json(explorer_info(b, dom, rel))
        if rest == ["explorer", "rows"]:
            return self.send_json(explorer_rows(b, dom, rel, q.get("table"), int(q.get("offset", 0)),
                                                min(int(q.get("limit", 100)), 500), q.get("sql")))
        if rest == ["explorer", "blob"]:
            data = explorer_blob(b, dom, rel, q.get("table", ""), int(q.get("rowid", 0)), q.get("col", ""))
            if q.get("raw") == "1":
                if data.startswith(b"\x1f\x8b"):
                    data = gzip.decompress(data)
                kind = sniff(data[:64])
                if kind == "heic" and HEIC_OK:
                    with tempfile.NamedTemporaryFile(dir=tmp_dir(), suffix=".heic", delete=False) as t:
                        t.write(data)
                    return self.send_bytes(heic_to_jpeg(pathlib.Path(t.name)), "image/jpeg")
                ctype = "image/png" if data.startswith(b"\x89PNG") else "image/gif" if data.startswith(b"GIF8") else "image/jpeg"
                return self.send_bytes(data, ctype if kind == "image" else "application/octet-stream")
            return self.send_json(decode_blob(data))
        if rest == ["raw"]:
            path = b.file(dom, rel)
            name = os.path.basename(rel) or "arquivo"
            if q.get("jpg") == "1" and path and HEIC_OK:
                return self.send_bytes(heic_to_jpeg(path), "image/jpeg",
                                       download=os.path.splitext(name)[0] + ".jpg" if q.get("dl") == "1" else None)
            if path and not os.path.splitext(name)[1]:
                with open(path, "rb") as f:
                    kind = sniff(f.read(64))
                name += {"image": ".jpg", "heic": ".heic", "pdf": ".pdf", "sqlite": ".sqlite", "plist": ".plist"}.get(kind, "")
            return self.send_file(path, name, download=q.get("dl") == "1")
        if rest[0] == "appicon" and len(rest) == 2:
            icon = app_catalog(b).get(rest[1], {}).get("icon")
            if not icon:
                raise KeyError("Sem ícone")
            return self.send_bytes(icon, "image/png")

        # contatos
        if rest == ["contacts"]:
            return self.send_json(contacts(b))
        if rest == ["contacts.vcf"]:
            people = contacts(b)
            if q.get("id"):
                people = [p for p in people if p["id"] == int(q["id"])]
            name = safe_name(people[0]["name"]) if len(people) == 1 else "Contatos do iPhone"
            return self.send_text(vcard(b, people), "text/vcard; charset=utf-8", download=f"{name}.vcf")
        if rest[0] == "contact" and len(rest) == 3 and rest[2] == "photo":
            img = contact_photo(b, int(rest[1]), full=q.get("thumb") != "1")
            if not img:
                raise KeyError("Sem foto")
            return self.send_bytes(img, "image/png" if img.startswith(b"\x89PNG") else "image/jpeg")

        # notas
        if rest == ["notes"]:
            idx = notes_index(b)
            return self.send_json({"folders": list(idx["folders"].values()),
                                   "notes": [{k: v for k, v in n.items() if not k.startswith("_")} for n in idx["notes"].values()]})
        if rest == ["notes", "search"]:
            nq = norm(q.get("q", "").strip())
            return self.send_json([n["id"] for n in notes_index(b)["notes"].values()
                                   if nq and nq in norm(n["title"] + "\n" + n["_text"])])
        if rest[0] == "notes" and len(rest) == 2:
            return self.send_json(note_detail(b, f"/api/b/{urllib.parse.quote(b.id)}", int(rest[1])))
        if rest[0] == "notes" and len(rest) == 3 and rest[2] == "export.txt":
            n = notes_index(b)["notes"].get(int(rest[1]))
            if not n:
                raise KeyError("Nota não encontrada")
            return self.send_text(n["_text"], download=f"{safe_name(n['title'])}.txt")
        if rest == ["photos"]:
            return self.send_json([{k: v for k, v in p.items() if not k.startswith("_")} for p in b.photos()])
        if rest[0] == "photo" and len(rest) == 3:
            p = b.photo(int(rest[1]))
            if not p:
                raise KeyError("Foto não encontrada")
            if rest[2] == "thumb":
                return self.send_file(b.file(CAMERA, p["_thumb"]), "thumb.jpg")
            src = b.file(CAMERA, p["_orig"])
            if rest[2] == "jpg":
                if not HEIC_OK:
                    raise KeyError("Instale pillow e pillow-heif para converter HEIC")
                if not src:
                    raise KeyError("Original não está no backup")
                name = os.path.splitext(p["name"])[0] + ".jpg"
                return self.send_bytes(heic_to_jpeg(src), "image/jpeg", download=name if q.get("dl") == "1" else None)
            return self.send_file(src, p["name"], download=q.get("dl") == "1")

        if rest[0] == "wa" and len(rest) >= 3:
            app = rest[1]
            if app not in WA_DOMAINS:
                raise KeyError("App desconhecido")
            mode = q.get("mode", "normal")
            if mode not in ("normal", "all", "starred"):
                raise ValueError("Modo inválido")
            if rest[2] == "chats":
                return self.send_json(b.wa_chats(app))
            if rest[2] == "search":
                return self.send_json(b.wa_search(app, q.get("q", ""), int(q["chat"]) if q.get("chat") else None))
            if rest[2] == "media" and len(rest) == 3:
                return self.send_json(b.wa_media_list(
                    app, int(q["chat"]) if q.get("chat") else None, q.get("cat", "all"), q.get("sort", "size"),
                    int(q.get("offset", 0)), min(int(q.get("limit", 120)), 500), mode))
            if rest[2] == "media" and len(rest) == 4:
                thumb = q.get("thumb") == "1"
                path, rel = b.wa_media(app, int(rest[3]), thumb)
                name = "thumb.jpg" if thumb else os.path.basename(rel or "arquivo")
                return self.send_file(path, name, download=q.get("dl") == "1")
            if rest[2] == "avatar" and len(rest) == 4:
                rel = b.avatars(app).get(rest[3].split("@")[0])
                return self.send_file(b.file(WA_DOMAINS[app], rel) if rel else None, "avatar.jpg")
            if rest[2] == "chat" and len(rest) == 5:
                chat = int(rest[3])
                if rest[4] == "messages":
                    return self.send_json(b.wa_messages(
                        app, chat, int(q.get("cursor", 0)), q.get("dir", "older"),
                        min(int(q.get("limit", 150)), 1000), mode, q.get("showdel") == "1"))
                if rest[4] == "position":
                    return self.send_json({"pos": b.wa_position(app, chat, int(q["msg"]))})
                if rest[4] == "export.txt":
                    name = safe_name(b._chat(app, chat)["name"])
                    return self.send_text(b.wa_export_txt(app, chat, mode), download=f"WhatsApp - {name}.txt")
        raise KeyError("Rota não encontrada")


# --- Exportações pela linha de comando ---------------------------------------
def export_photos(backup, dest, to_jpg=False):
    if to_jpg and not HEIC_OK:
        sys.exit("Para --jpg instale: python -m pip install pillow pillow-heif")
    dest = out_dir(dest)
    dest.mkdir(parents=True, exist_ok=True)
    photos = backup.photos()
    done = skipped = 0
    for i, p in enumerate(photos, 1):
        src = backup.file(CAMERA, p["_orig"])
        if not src:
            skipped += 1
            continue
        stamp = dt.datetime.fromtimestamp(p["date"]).strftime("%Y-%m-%d_%H%M%S") if p["date"] else "sem-data"
        name = p["name"]
        convert = to_jpg and name.lower().endswith((".heic", ".heif"))
        if convert:
            name = os.path.splitext(name)[0] + ".jpg"
        target = dest / f"{stamp}_{name}"
        if not target.exists():
            if convert:
                target.write_bytes(heic_to_jpeg(src))
            else:
                shutil.copy2(src, target)
            if p["date"]:
                os.utime(target, (p["date"], p["date"]))
        done += 1
        if i % 200 == 0:
            print(f"  {i}/{len(photos)}...")
    print(f"Exportadas {done} fotos/vídeos para {str(dest).removeprefix(chr(92) * 2 + '?' + chr(92))} ({skipped} sem original no backup).")


def export_whatsapp(backup, app, dest):
    data = backup.wa_chats(app)
    names = {f["id"]: f["name"] for f in data["folders"]}
    root = out_dir(dest) / APP_NAMES[app]
    used = set()
    n_chats = n_media = 0
    for c in data["chats"]:
        if c["deleted"] and not c["keep"]:
            continue  # na lixeira
        base = root / safe_name(names.get(c["folder"], "Sem pasta")) / safe_name(c["name"])
        cdir, k = base, 2
        while str(cdir).lower() in used:
            cdir, k = base.with_name(f"{base.name} ({k})"), k + 1
        used.add(str(cdir).lower())
        cdir.mkdir(parents=True, exist_ok=True)
        lines = []
        for m in backup.iter_messages(app, c["id"]):
            lines.append(fmt_line(m))
            md = m["media"]
            if md and md["full"]:
                src, _ = backup.wa_media(app, md["id"])
                if src:
                    mdir = cdir / "midias"
                    mdir.mkdir(exist_ok=True)
                    target = mdir / safe_name(md["name"], 120)
                    if not target.exists():
                        shutil.copy2(src, target)
                        if m["date"]:
                            os.utime(target, (m["date"], m["date"]))
                    n_media += 1
        (cdir / "conversa.txt").write_text("\n".join(lines) + "\n", "utf-8")
        n_chats += 1
        if n_chats % 50 == 0:
            print(f"  {n_chats} conversas...")
    print(f"{APP_NAMES[app]}: {n_chats} conversas e {n_media} mídias exportadas para {str(root).removeprefix(chr(92) * 2 + '?' + chr(92))}")


def main():
    global STATE
    ap = argparse.ArgumentParser(description="Explorador de backups do iPhone (Fotos, WhatsApp, Notas, Contatos e Apps)")
    ap.add_argument("--backup-dir", action="append", help="pasta com backups (pode repetir)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dados", metavar="PASTA", help="onde guardar suas pastas/exclusões (padrão: %%APPDATA%%\\Garimpo)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--export-photos", metavar="PASTA", help="copia todas as fotos/vídeos para PASTA e sai")
    ap.add_argument("--jpg", action="store_true", help="com --export-photos: converte HEIC para JPG")
    ap.add_argument("--export-whatsapp", metavar="PASTA",
                    help="exporta as conversas (respeitando pastas e exclusões) para PASTA e sai")
    ap.add_argument("--app", choices=["wa", "biz", "ambos"], default="ambos")
    ap.add_argument("--backup", help="ID do backup (padrão: o mais recente)")
    ap.add_argument("--janela", action="store_true",
                    help="modo aplicativo: janela própria, sem console, fecha sozinho com a janela (usado pelos atalhos)")
    ap.add_argument("--criar-atalhos", action="store_true",
                    help="cria atalhos com ícone na Área de Trabalho, no Menu Iniciar e nesta pasta")
    args = ap.parse_args()

    DADOS = pathlib.Path(args.dados).expanduser() if args.dados else default_data_dir()
    if args.criar_atalhos:
        return create_shortcuts()
    if args.janela:
        # sem console (pythonw): mensagens vão para dados/app.log
        DADOS.mkdir(parents=True, exist_ok=True)
        log = open(DADOS / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
        sys._ibv_log = True
        print(f"\n--- {dt.datetime.now():%d/%m/%Y %H:%M:%S} ---")
        if already_running(args.port):
            return open_ui(f"http://127.0.0.1:{args.port}/")  # já aberto: só mostra a janela

    STATE = State(DADOS / "organizacao.json")
    backups = find_backups(args.backup_dir or default_roots())
    if not backups:
        fail("Nenhum backup do iPhone foi encontrado neste computador.\n\n"
             "Faça um backup pelo app Dispositivos Apple (ou iTunes) sem criptografia e tente de novo.")
    # backups normais primeiro (mais recente no topo); arquivados depois
    ordered = sorted(backups.values(), key=lambda b: (not b.is_archived, b.info.get("Last Backup Date") or dt.datetime.min),
                     reverse=True)
    Handler.backups = {b.id: b for b in ordered}

    if args.export_photos or args.export_whatsapp:
        b = backups.get(args.backup) if args.backup else ordered[0]
        if not b:
            sys.exit(f"Backup {args.backup} não encontrado.")
        if args.export_photos:
            print(f"Exportando fotos de '{b.info.get('Device Name')}' ({b.id})...")
            export_photos(b, args.export_photos, args.jpg)
        if args.export_whatsapp:
            for app in (["wa", "biz"] if args.app == "ambos" else [args.app]):
                if b.file(WA_DOMAINS[app], "ChatStorage.sqlite"):
                    export_whatsapp(b, app, args.export_whatsapp)
        return

    try:
        srv = Server(("127.0.0.1", args.port), Handler)
    except OSError:
        fail(f"A porta {args.port} está ocupada por outro programa.")
    Handler.allowed_hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
    url = f"http://127.0.0.1:{args.port}/"
    print(f"{len(backups)} backup(s) encontrado(s). Abrindo {url}  (Ctrl+C para sair)")
    if args.janela:
        def watchdog():
            # sem a janela aberta (ela avisa a cada 15 s), o app se encerra sozinho
            while True:
                time.sleep(10)
                if time.time() - Handler.last_seen > IDLE_EXIT:
                    srv.shutdown()
                    return
        threading.Thread(target=watchdog, daemon=True).start()
    if not HEIC_OK:
        print("Dica: para converter HEIC em JPG, instale: python -m pip install pillow pillow-heif")
    def warm(b):
        # prepara os índices do backup mais recente em segundo plano
        try:
            for app in WA_DOMAINS:
                if not b.encrypted and b.file(WA_DOMAINS[app], "ChatStorage.sqlite"):
                    b.wa_media_all(app)
                    b.avatars(app)
                    b.wa_text_index(app)
        except Exception as e:
            print(f"Aviso ao preparar índices: {e}", file=sys.stderr)
    threading.Thread(target=warm, args=(ordered[0],), daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.5, open_ui, (url, args.janela)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")
    srv.server_close()


# --- Modo aplicativo (atalhos, janela própria, encerramento) -----------------
IDLE_EXIT = 150  # segundos sem a janela aberta até o app fechar sozinho
APP_DIR = pathlib.Path(__file__).resolve().parent


class Server(ThreadingHTTPServer):
    # no Windows, SO_REUSEADDR deixaria dois servidores ouvirem a mesma porta
    allow_reuse_address = False
    daemon_threads = True


def fail(msg):
    """Erro fatal: no modo sem console (pythonw) aparece numa caixa de mensagem."""
    if sys.stdout is None or getattr(sys, "_ibv_log", False):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "Garimpo", 0x10)
        except Exception:
            pass
    sys.exit(msg)


def find_app_browser():
    """Edge ou Chrome, para abrir o app numa janela própria (--app), sem abas nem barra de endereço."""
    cands = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    return next((c for c in cands if os.path.exists(c)), None)


def open_ui(url, as_window=True):
    exe = find_app_browser() if as_window else None
    if exe:
        import subprocess
        subprocess.Popen([exe, f"--app={url}", "--window-size=1400,900"], close_fds=True)
    else:
        webbrowser.open(url)


def already_running(port):
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.load(r).get("app") == "garimpo"
    except Exception:
        return False


def make_icon(path):
    """Gera o ícone do app (precisa do Pillow)."""
    from PIL import Image, ImageDraw
    S = 256
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for y in range(S):  # fundo em degradê azul
        t = y / S
        d.line([(0, y), (S, y)], fill=(int(30 + 20 * t), int(120 - 50 * t), int(255 - 60 * t), 255))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=56, fill=255)
    im.putalpha(mask)
    d.rounded_rectangle([70, 34, 166, 222], radius=20, fill=(255, 255, 255, 255))   # celular
    d.rounded_rectangle([80, 56, 156, 196], radius=6, fill=(40, 90, 200, 255))      # tela
    d.ellipse([110, 204, 126, 216], fill=(40, 90, 200, 255))                         # botão
    d.ellipse([132, 118, 228, 210], fill=(37, 211, 102, 255), outline=(255, 255, 255, 255), width=8)  # balão
    d.polygon([(140, 196), (128, 226), (166, 206)], fill=(37, 211, 102, 255))
    for x in (156, 180, 204):
        d.ellipse([x - 7, 157, x + 7, 171], fill=(255, 255, 255, 255))
    im.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def create_shortcuts():
    """Atalhos na Área de Trabalho, no Menu Iniciar e na pasta do app (Windows)."""
    import subprocess
    pyw = pathlib.Path(sys.executable).with_name("pythonw.exe")
    if not pyw.exists():
        pyw = pathlib.Path(sys.executable)
    icon = APP_DIR / "garimpo.ico"
    try:
        make_icon(icon)
    except Exception as e:
        print(f"Não consegui gerar o ícone ({e}); os atalhos usarão o ícone do Python.")
        icon = pyw
    ps = r"""
$ws = New-Object -ComObject WScript.Shell
$targets = @([Environment]::GetFolderPath('Desktop'), (Join-Path ([Environment]::GetFolderPath('Programs')) ''), $env:GARIMPO_DIR)
foreach ($dir in $targets) {
  $lnk = $ws.CreateShortcut((Join-Path $dir 'Garimpo.lnk'))
  $lnk.TargetPath = $env:GARIMPO_PYW
  $lnk.Arguments = '"' + $env:GARIMPO_SCRIPT + '" --janela'
  $lnk.WorkingDirectory = $env:GARIMPO_DIR
  $lnk.IconLocation = $env:GARIMPO_ICON + ',0'
  $lnk.Description = 'Explorador de backups do iPhone (Fotos, WhatsApp, Notas, Contatos e Apps)'
  $lnk.Save()
  Write-Output (Join-Path $dir 'Garimpo.lnk')
}
"""
    env = dict(os.environ, GARIMPO_PYW=str(pyw), GARIMPO_SCRIPT=str(APP_DIR / "garimpo.py"),
               GARIMPO_DIR=str(APP_DIR), GARIMPO_ICON=str(icon))
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        sys.exit("Falha ao criar atalhos: " + r.stderr)
    print("Atalhos criados:")
    for line in r.stdout.splitlines():
        if line.strip():
            print("  " + line.strip())


INDEX_HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Garimpo</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'%3E%3Ctext y='.9em' font-size='85'%3E%E2%9B%8F%EF%B8%8F%3C/text%3E%3C/svg%3E">
<style>
:root {
  --bg: #f4f5f7; --panel: #ffffff; --panel-2: #f0f1f4; --text: #1d1f23; --muted: #6b7079;
  --line: #e2e4e9; --accent: #0a7cff; --danger: #d93025; --me: #d9fdd3; --them: #ffffff; --chat-bg: #efeae2;
  --hl: #fff3a3; --shadow: 0 1px 2px rgba(0,0,0,.08);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #111317; --panel: #1a1d22; --panel-2: #22262d; --text: #e7e9ec; --muted: #9096a0;
    --line: #2c3038; --accent: #4c9dff; --danger: #ff6b5e; --me: #144d37; --them: #202c33; --chat-bg: #0b141a;
    --hl: #5c5200; --shadow: 0 1px 2px rgba(0,0,0,.4);
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body { background: var(--bg); color: var(--text); font: 14px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; display: flex; flex-direction: column; }
button, select, input { font: inherit; color: inherit; }
select { background: var(--panel-2); border: 1px solid var(--line); border-radius: 6px; padding: 4px 6px; }
mark { background: var(--hl); color: inherit; border-radius: 2px; }
header { display: flex; align-items: center; gap: 16px; padding: 10px 16px; background: var(--panel); border-bottom: 1px solid var(--line); flex-wrap: wrap; }
header h1 { font-size: 16px; margin: 0; font-weight: 650; }
#device { color: var(--muted); font-size: 13px; }
nav { display: flex; gap: 4px; margin-left: auto; }
nav button { border: 0; background: transparent; padding: 6px 12px; border-radius: 6px; cursor: pointer; color: var(--muted); }
nav button.on { background: var(--panel-2); color: var(--text); font-weight: 600; }
main { flex: 1; min-height: 0; display: flex; }
.view { flex: 1; min-height: 0; min-width: 0; display: none; }
.view.on { display: flex; }
.empty { margin: auto; color: var(--muted); text-align: center; padding: 24px; }
.btn { border: 1px solid var(--line); background: var(--panel-2); padding: 5px 11px; border-radius: 6px; cursor: pointer; text-decoration: none; color: var(--text); font-size: 13px; white-space: nowrap; }
.btn:hover { border-color: var(--muted); }
.btn.danger { color: var(--danger); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.chip { border: 1px solid var(--line); background: var(--panel); padding: 4px 12px; border-radius: 999px; cursor: pointer; font-size: 13px; }
.chip.on { background: var(--accent); border-color: var(--accent); color: #fff; }
.chip small { opacity: .75; }
.toolbar { display: flex; gap: 6px; padding: 10px 16px; align-items: center; flex-wrap: wrap; border-bottom: 1px solid var(--line); background: var(--panel); }
.toolbar .count { margin-left: auto; color: var(--muted); font-size: 13px; }

/* Fotos */
#photos { flex-direction: column; }
#grid { flex: 1; overflow-y: auto; padding: 8px 16px 32px; }
.month { font-weight: 650; margin: 18px 0 8px; font-size: 15px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 3px; }
.tile { position: relative; aspect-ratio: 1; background: var(--panel-2); cursor: pointer; overflow: hidden; }
.tile img { width: 100%; height: 100%; object-fit: cover; display: block; }
.tile .badge { position: absolute; right: 4px; bottom: 4px; background: rgba(0,0,0,.65); color: #fff; font-size: 11px; padding: 1px 5px; border-radius: 4px; }
.tile .fav { position: absolute; left: 4px; bottom: 2px; color: #fff; text-shadow: 0 0 3px #000; }
.tile.missing img { opacity: .55; }
.tile .icon { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 4px; font-size: 30px; padding: 6px; text-align: center; }
.tile .icon small { font-size: 11px; color: var(--muted); word-break: break-all; max-height: 3em; overflow: hidden; }
.tile .who { position: absolute; left: 0; right: 0; top: 0; background: linear-gradient(rgba(0,0,0,.6), transparent); color: #fff; font-size: 11px; padding: 3px 5px 10px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tile .ck { position: absolute; top: 4px; right: 4px; width: 18px; height: 18px; display: none; }
.tile:hover .ck, .tile .ck:checked, .sel-mode .tile .ck { display: block; }
.tile.sel { outline: 3px solid var(--accent); outline-offset: -3px; }

/* WhatsApp */
.folders { width: 190px; flex: none; background: var(--panel); border-right: 1px solid var(--line); overflow-y: auto; padding: 8px 6px; display: flex; flex-direction: column; gap: 2px; }
.folders h4 { margin: 6px 8px; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.fitem { display: flex; align-items: center; gap: 8px; padding: 7px 8px; border-radius: 6px; cursor: pointer; border: 0; background: transparent; width: 100%; text-align: left; }
.fitem:hover { background: var(--panel-2); }
.fitem.on { background: var(--panel-2); font-weight: 600; }
.fitem .fn { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.fitem .fc { color: var(--muted); font-size: 12px; }
.fitem .fx { display: none; color: var(--muted); font-size: 12px; padding: 0 2px; }
.fitem:hover .fx { display: inline; }
.fitem.drop { outline: 2px dashed var(--accent); }
.chats { width: 360px; max-width: 40%; flex: none; display: flex; flex-direction: column; background: var(--panel); border-right: 1px solid var(--line); }
.fselect { display: none; margin: 8px 10px 0; }
.searchrow { padding: 10px 10px 6px; display: flex; flex-direction: column; gap: 6px; }
.searchrow input { padding: 7px 10px; border-radius: 8px; border: 1px solid var(--line); background: var(--panel-2); }
.seg { display: flex; background: var(--panel-2); border-radius: 7px; padding: 2px; }
.seg button { flex: 1; border: 0; background: transparent; padding: 4px 8px; border-radius: 5px; cursor: pointer; font-size: 13px; color: var(--muted); }
.seg button.on { background: var(--panel); color: var(--text); box-shadow: var(--shadow); font-weight: 600; }
.listbar { display: flex; align-items: center; gap: 8px; padding: 4px 12px 8px; font-size: 13px; color: var(--muted); flex-wrap: wrap; }
.listbar select { margin-left: auto; }
.bulk, .folderbar { display: flex; align-items: center; gap: 6px; padding: 6px 10px; border-top: 1px solid var(--line); font-size: 13px; flex-wrap: wrap; }
.bulk { background: var(--panel-2); }
.bulk[hidden], .folderbar[hidden] { display: none; }
.chatlist { overflow-y: auto; flex: 1; border-top: 1px solid var(--line); }
.chat { padding: 9px 12px; border-bottom: 1px solid var(--line); cursor: pointer; display: flex; gap: 10px; align-items: center; }
.chat:hover { background: var(--panel-2); }
.chat.on { background: var(--panel-2); box-shadow: inset 3px 0 var(--accent); }
.chat.gone { opacity: .6; }
.chat .ck { flex: none; margin: 0; }
.av { width: 40px; height: 40px; border-radius: 50%; flex: none; background: var(--panel-2); display: flex; align-items: center; justify-content: center; font-weight: 650; color: #fff; overflow: hidden; font-size: 15px; }
.av img { width: 100%; height: 100%; object-fit: cover; }
.ci { min-width: 0; flex: 1; }
.ci .r1 { display: flex; gap: 8px; }
.ci .n { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }
.ci .d { color: var(--muted); font-size: 12px; flex: none; }
.ci .l { color: var(--muted); font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.ci .meta { font-size: 11.5px; color: var(--muted); display: flex; gap: 8px; flex-wrap: wrap; margin-top: 2px; }
.tag { background: var(--panel-2); border: 1px solid var(--line); padding: 0 6px; border-radius: 4px; }
.result { padding: 9px 12px; border-bottom: 1px solid var(--line); cursor: pointer; }
.result:hover { background: var(--panel-2); }
.result .r1 { display: flex; justify-content: space-between; gap: 8px; font-size: 12.5px; }
.result .r1 b { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.result .r1 span { color: var(--muted); flex: none; }
.result .s { font-size: 13px; color: var(--muted); margin-top: 2px; word-wrap: break-word; }
.conv { flex: 1; display: flex; flex-direction: column; min-width: 0; background: var(--chat-bg); position: relative; }
.convhead { display: flex; align-items: center; gap: 10px; padding: 8px 14px; background: var(--panel); border-bottom: 1px solid var(--line); flex-wrap: wrap; }
.convhead .tt { min-width: 0; flex: 1; }
.convhead .t { font-weight: 650; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.convhead .s { color: var(--muted); font-size: 12px; }
.convhead .tools { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.menu { position: relative; }
.menu > summary { list-style: none; }
.menu > summary::-webkit-details-marker { display: none; }
.menu .pop { position: absolute; right: 0; top: calc(100% + 4px); background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 6px 24px rgba(0,0,0,.18); padding: 4px; z-index: 5; min-width: 220px; display: flex; flex-direction: column; }
.menu .pop button, .menu .pop a { text-align: left; border: 0; background: transparent; padding: 7px 10px; border-radius: 5px; cursor: pointer; color: var(--text); text-decoration: none; font-size: 13px; }
.menu .pop button:hover, .menu .pop a:hover { background: var(--panel-2); }
.menu .pop .danger { color: var(--danger); }
.banner { padding: 8px 14px; background: var(--hl); font-size: 13px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.insearch { display: flex; gap: 6px; padding: 8px 14px; background: var(--panel); border-bottom: 1px solid var(--line); }
.insearch input { flex: 1; padding: 6px 10px; border-radius: 6px; border: 1px solid var(--line); background: var(--panel-2); }
.inresults { position: absolute; left: 14px; right: 14px; z-index: 4; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 6px 24px rgba(0,0,0,.18); max-height: 55%; overflow-y: auto; }
.msgs { flex: 1; overflow-y: auto; padding: 12px 6%; display: flex; flex-direction: column; gap: 3px; }
.day { align-self: center; background: var(--panel); color: var(--muted); font-size: 12px; padding: 3px 10px; border-radius: 6px; margin: 10px 0; box-shadow: var(--shadow); }
.sys { align-self: center; color: var(--muted); font-size: 12px; font-style: italic; padding: 2px 8px; }
.msg { position: relative; max-width: min(75%, 560px); padding: 6px 9px 4px; border-radius: 8px; background: var(--them); box-shadow: var(--shadow); align-self: flex-start; word-wrap: break-word; white-space: pre-wrap; }
.msg.me { background: var(--me); align-self: flex-end; }
.msg.del { opacity: .45; outline: 1px dashed var(--danger); }
.msg.hl { animation: hl 2.5s ease-out; }
@keyframes hl { 0%, 40% { box-shadow: 0 0 0 4px var(--accent); } 100% { box-shadow: var(--shadow); } }
.msg .who { font-size: 12.5px; font-weight: 650; margin-bottom: 2px; }
.msg .time { float: right; margin: 6px 0 0 10px; font-size: 11px; color: var(--muted); }
.msg .time .st { color: #e0a800; }
.msg .acts { position: absolute; top: -12px; right: 6px; display: none; gap: 2px; background: var(--panel); border: 1px solid var(--line); border-radius: 6px; padding: 1px; box-shadow: var(--shadow); white-space: normal; }
.msg:hover .acts { display: flex; }
.msg .acts button { border: 0; background: transparent; cursor: pointer; padding: 2px 6px; border-radius: 4px; font-size: 13px; }
.msg .acts button:hover { background: var(--panel-2); }
.msg img.media { display: block; max-width: 100%; max-height: 320px; border-radius: 5px; cursor: pointer; margin-bottom: 3px; }
.msg img.sticker { max-width: 150px; }
.msg video { display: block; max-width: 100%; max-height: 320px; border-radius: 5px; }
.msg audio { display: block; width: 260px; max-width: 100%; }
.msg .doc { display: inline-block; padding: 6px 10px; background: rgba(127,127,127,.12); border-radius: 6px; color: inherit; }
.msg .miss { color: var(--muted); font-style: italic; font-size: 13px; }
.mpane { flex: 1; min-height: 0; display: flex; flex-direction: column; background: var(--bg); }
.mpane .mgrid { flex: 1; overflow-y: auto; padding: 10px 14px 30px; }
.mpane .tiles { grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 6px; }
.mpane .tile { border-radius: 6px; }

/* Notas, Contatos e Apps */
.col { flex: none; display: flex; flex-direction: column; background: var(--panel); border-right: 1px solid var(--line); min-height: 0; }
.col .list { flex: 1; overflow-y: auto; border-top: 1px solid var(--line); }
.item { padding: 9px 12px; border-bottom: 1px solid var(--line); cursor: pointer; display: flex; gap: 10px; align-items: center; min-width: 0; }
.item:hover { background: var(--panel-2); }
.item.on { background: var(--panel-2); box-shadow: inset 3px 0 var(--accent); }
.item .ci .n { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.item .ic { width: 32px; height: 32px; border-radius: 8px; flex: none; display: flex; align-items: center; justify-content: center; font-size: 18px; background: var(--panel-2); overflow: hidden; }
.item .ic img { width: 100%; height: 100%; object-fit: cover; }
.item .sz { color: var(--muted); font-size: 12px; flex: none; }
.detail { flex: 1; min-width: 0; overflow-y: auto; background: var(--bg); }
.detail .inner { max-width: 820px; margin: 0 auto; padding: 22px 28px 60px; }
.dhead { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
.dhead h2 { margin: 0; font-size: 20px; flex: 1; min-width: 0; word-wrap: break-word; }
.meta-line { color: var(--muted); font-size: 12.5px; margin-bottom: 16px; }
.notebody { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 18px 22px; line-height: 1.55; word-wrap: break-word; }
.notebody h1 { font-size: 22px; margin: 4px 0 10px; } .notebody h2 { font-size: 18px; margin: 12px 0 6px; } .notebody h3 { font-size: 16px; margin: 10px 0 4px; }
.notebody p { margin: 0 0 2px; min-height: 1em; }
.notebody pre { background: var(--panel-2); padding: 2px 8px; margin: 0; border-radius: 4px; white-space: pre-wrap; }
.notebody ul.dash { list-style: "– "; }
.notebody .nchk { padding: 1px 0; } .notebody .nchk.done { color: var(--muted); text-decoration: line-through; }
.notebody .nimg { max-width: 100%; max-height: 420px; border-radius: 6px; display: block; margin: 6px 0; cursor: pointer; }
.notebody .ngallery { display: flex; gap: 6px; flex-wrap: wrap; } .notebody .ngallery .nimg { max-height: 200px; max-width: 48%; }
.notebody .ncard { display: inline-block; padding: 8px 12px; background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px; margin: 4px 0; color: var(--text); text-decoration: none; }
.notebody .ncap { font-size: 12px; color: var(--muted); } .notebody .ntag { color: var(--accent); }
.badge2 { font-size: 11px; padding: 1px 6px; border-radius: 4px; background: var(--panel-2); border: 1px solid var(--line); color: var(--muted); }
.cdetail .big { width: 96px; height: 96px; border-radius: 50%; font-size: 36px; }
.cdetail .sec { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; margin-bottom: 12px; }
.cdetail .row2 { display: flex; gap: 12px; padding: 9px 14px; border-bottom: 1px solid var(--line); }
.cdetail .row2:last-child { border-bottom: 0; }
.cdetail .row2 .k { width: 120px; flex: none; color: var(--muted); font-size: 13px; }
.cdetail .row2 .v { flex: 1; min-width: 0; word-wrap: break-word; white-space: pre-wrap; }
.crumbs { display: flex; gap: 4px; flex-wrap: wrap; padding: 8px 12px; font-size: 13px; align-items: center; }
.crumbs a { color: var(--accent); cursor: pointer; text-decoration: none; }
.prev { padding: 16px 20px 40px; }
.prev pre { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px; overflow: auto; max-height: 70vh; font-size: 12.5px; white-space: pre-wrap; word-break: break-all; }
.prev img, .prev video { max-width: 100%; max-height: 70vh; border-radius: 6px; }
.prev iframe { width: 100%; height: 75vh; border: 1px solid var(--line); border-radius: 6px; background: #fff; }
.grid-wrap { overflow: auto; max-height: 62vh; border: 1px solid var(--line); border-radius: 8px; background: var(--panel); }
table.grid { border-collapse: collapse; font-size: 12.5px; }
table.grid th, table.grid td { border-bottom: 1px solid var(--line); border-right: 1px solid var(--line); padding: 4px 8px; text-align: left; vertical-align: top; max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
table.grid th { position: sticky; top: 0; background: var(--panel-2); z-index: 1; }
table.grid td.num { text-align: right; font-variant-numeric: tabular-nums; }
table.grid .blob { color: var(--accent); cursor: pointer; }
table.grid .null { color: var(--muted); font-style: italic; }
.sqlbox { display: flex; gap: 6px; margin: 10px 0; }
.sqlbox[hidden] { display: none; }
.sqlbox textarea { flex: 1; min-height: 54px; font: 12.5px ui-monospace, Consolas, monospace; padding: 6px 8px; border: 1px solid var(--line); border-radius: 6px; background: var(--panel); color: var(--text); }
@media (max-width: 1100px) { .apps-view .files { width: 300px !important; } .apps-view .applist { width: 230px !important; } }
@media (max-width: 760px) {
  .split .col { width: 100% !important; }
  .split.open > .col, .split.open2 > .col { display: none; }
  .split:not(.open):not(.open2) > .detail { display: none; }
  .apps-view:not(.open2) > .files { display: none; }
  .apps-view.open2 > .files { display: flex; width: 100% !important; }
  .apps-view.open2 > .detail { display: none; }
  .back2 { display: inline-block !important; }
}

/* Visualizador e diálogos */
#lb { position: fixed; inset: 0; background: rgba(0,0,0,.92); display: none; flex-direction: column; z-index: 10; color: #fff; }
#lb.on { display: flex; }
#lb .bar { display: flex; gap: 8px; padding: 10px 16px; align-items: center; flex-wrap: wrap; }
#lb .bar .cap { flex: 1; font-size: 13px; opacity: .85; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 120px; }
#lb .bar button, #lb .bar a { background: rgba(255,255,255,.12); border: 0; color: #fff; padding: 6px 12px; border-radius: 6px; cursor: pointer; text-decoration: none; font-size: 13px; }
#lb .bar .danger { color: #ff8a80; }
#lb .stage { flex: 1; min-height: 0; display: flex; align-items: center; justify-content: center; position: relative; padding: 0 56px 16px; }
#lb .stage img, #lb .stage video { max-width: 100%; max-height: 100%; object-fit: contain; }
#lb .stage .big { font-size: 64px; text-align: center; }
#lb .stage .big div { font-size: 14px; margin-top: 10px; opacity: .8; }
#lb .nav { position: absolute; top: 50%; transform: translateY(-50%); background: rgba(255,255,255,.12); border: 0; color: #fff; font-size: 26px; width: 42px; height: 64px; border-radius: 8px; cursor: pointer; }
#lb .prev { left: 8px; } #lb .next { right: 8px; }
#lb .note { position: absolute; bottom: 20px; left: 50%; transform: translateX(-50%); background: rgba(0,0,0,.6); padding: 4px 10px; border-radius: 6px; font-size: 12px; }
dialog { border: 1px solid var(--line); border-radius: 10px; background: var(--panel); color: var(--text); max-width: 440px; width: calc(100% - 32px); padding: 0; }
dialog > div { padding: 18px; }
dialog::backdrop { background: rgba(0,0,0,.45); }
dialog h3 { margin: 0 0 8px; font-size: 16px; }
dialog p { margin: 0 0 12px; color: var(--muted); }
dialog label { display: flex; gap: 8px; align-items: center; margin-bottom: 14px; }
dialog .row { display: flex; justify-content: flex-end; gap: 8px; }
#dead { position: fixed; inset: 0; background: var(--bg); z-index: 30; display: none; align-items: center; justify-content: center; text-align: center; padding: 24px; }
#dead.on { display: flex; }
#dead p { color: var(--muted); max-width: 420px; }
#toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); background: #222; color: #fff; padding: 8px 14px; border-radius: 8px; font-size: 13px; z-index: 20; display: none; }

@media (max-width: 1100px) { .folders { display: none; } .fselect { display: block; } }
@media (max-width: 760px) {
  .wa .chats { width: 100%; max-width: none; }
  .wa.open .chats { display: none; }
  .wa:not(.open) .conv { display: none; }
  .msgs { padding: 12px 10px; }
  .back { display: inline-block !important; }
  .tiles { grid-template-columns: repeat(auto-fill, minmax(90px, 1fr)); }
  .mpane .tiles { grid-template-columns: repeat(auto-fill, minmax(100px, 1fr)); }
}
</style>
</head>
<body>
<header>
  <h1>Garimpo</h1>
  <div id="device">Carregando…</div>
  <nav id="tabs"></nav>
  <button id="quit" class="btn" title="Fechar o Garimpo">⏻ Sair</button>
</header>
<div id="dead"><div><div style="font-size:40px">👋</div><h2>O Garimpo foi encerrado</h2>
  <p>Pode fechar esta janela. Para abrir de novo, use o atalho <b>Garimpo</b> na Área de Trabalho ou no Menu Iniciar.</p></div></div>
<main>
  <section class="view" id="photos">
    <div class="toolbar">
      <button class="chip on" data-f="all">Tudo</button>
      <button class="chip" data-f="photo">Fotos</button>
      <button class="chip" data-f="video">Vídeos</button>
      <button class="chip" data-f="fav">Favoritas</button>
      <button class="chip" data-f="hidden">Ocultas</button>
      <span class="count" id="pcount"></span>
    </div>
    <div id="grid"></div>
  </section>
</main>
<div id="lb"><div class="bar"></div><div class="stage"></div></div>
<dialog id="dlg"><div>
  <h3></h3><p></p>
  <label class="chk"><input type="checkbox"> <span></span></label>
  <div class="row"><button type="button" class="btn" value="cancel">Cancelar</button><button type="button" class="btn primary ok" value="ok">OK</button></div>
</div></dialog>
<div id="toast"></div>
<script>
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtDate = t => t ? new Date(t * 1000).toLocaleDateString("pt-BR") : "";
const fmtDT = t => t ? new Date(t * 1000).toLocaleString("pt-BR") : "";
const fmtTime = t => t ? new Date(t * 1000).toLocaleTimeString("pt-BR", {hour: "2-digit", minute: "2-digit"}) : "";
const fmtDay = t => new Date(t * 1000).toLocaleDateString("pt-BR", {weekday: "long", day: "numeric", month: "long", year: "numeric"});
const fmtMonth = t => t ? new Date(t * 1000).toLocaleDateString("pt-BR", {month: "long", year: "numeric"}) : "Sem data";
const fmtDur = s => s == null ? "" : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
const fmtSize = n => !n ? "0 KB" : n >= 1e9 ? (n / 1e9).toFixed(2).replace(".", ",") + " GB" : n >= 1e6 ? (n / 1e6).toFixed(1).replace(".", ",") + " MB" : Math.max(1, Math.round(n / 1e3)) + " KB";
const num = n => (n || 0).toLocaleString("pt-BR");
async function api(p) {
  const r = await fetch(p);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
async function post(p, body) {
  const r = await fetch(p, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
let toastT;
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.style.display = "block";
  clearTimeout(toastT); toastT = setTimeout(() => t.style.display = "none", 3500);
}
function ask({title, text = "", check = null, ok = "OK", danger = false}) {
  const d = $("#dlg");
  $("h3", d).textContent = title; $("p", d).textContent = text;
  const lab = $("label", d), cb = $("input", d);
  lab.style.display = check ? "" : "none"; cb.checked = !!(check && check.value);
  if (check) $("span", lab).textContent = check.label;
  const b = $(".ok", d); b.textContent = ok; b.className = "btn ok " + (danger ? "danger" : "primary");
  return new Promise(res => {
    const done = v => { d.onclick = d.onkeydown = null; if (d.open) d.close(); res(v); };
    d.onclick = e => {
      if (e.target === b) done({checked: cb.checked});
      else if (e.target.value === "cancel" || e.target === d) done(null);  // clicar fora fecha
    };
    d.onkeydown = e => { if (e.key === "Escape") { e.preventDefault(); done(null); } };
    d.showModal();
  });
}
const WEB_IMG = /\.(jpe?g|png|gif|webp)$/i;
const HEIC = /\.(heic|heif)$/i;

let B = null, base = "";
const views = {};

async function init() {
  const list = await api("/api/backups");
  if (!list.length) { $("#device").textContent = "Nenhum backup encontrado"; return; }
  const label = b => `${b.archived ? "📦 Arquivado · " : ""}${b.name} · ${b.model} · iOS ${b.ios} · ${b.date ? new Date(b.date).toLocaleString("pt-BR") : ""}`;
  if (list.length > 1) {
    $("#device").innerHTML = `<select>${list.map(b => `<option value="${b.id}">${esc(label(b))}</option>`).join("")}</select>`;
    $("#device select").onchange = e => select(list.find(b => b.id === e.target.value));
  }
  select(list[0]);
  function select(b) {
    if (list.length === 1) $("#device").textContent = label(b);
    B = b; base = `/api/b/${b.id}`;
    $$(".view.dyn").forEach(v => v.remove());
    for (const k in views) delete views[k];
    if (b.encrypted) { $("#tabs").innerHTML = ""; $("#grid").innerHTML = `<div class="empty">Este backup é criptografado — ainda não suportado.</div>`; return; }
    const tabs = [["photos", "Fotos"]];
    if (b.apps.includes("wa")) tabs.push(["wa", "WhatsApp"]);
    if (b.apps.includes("biz")) tabs.push(["biz", "WhatsApp Business"]);
    if (b.notes) tabs.push(["notes", "Notas"]);
    if (b.contacts) tabs.push(["contacts", "Contatos"]);
    tabs.push(["apps", "Apps"]);
    $("#tabs").innerHTML = tabs.map(([k, t]) => `<button data-k="${k}">${t}</button>`).join("");
    $("#tabs").onclick = e => e.target.dataset.k && show(e.target.dataset.k);
    show("photos");
  }
}
function show(k) {
  $$("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.k === k));
  $$(".view").forEach(v => v.classList.remove("on"));
  if (k === "photos") { $("#photos").classList.add("on"); if (!views.photos) views.photos = loadPhotos(); }
  else {
    if (!views[k]) views[k] = ({notes: makeNotes, contacts: makeContacts, apps: makeApps}[k] || (() => makeWA(k)))();
    views[k].el.classList.add("on");
  }
}

/* ---------- Visualizador (lightbox) ---------- */
const lb = $("#lb");
const LB = {list: null, i: 0, render: null};
function openLb(list, i, render) { Object.assign(LB, {list, i, render}); drawLb(); lb.classList.add("on"); }
function drawLb() {
  const r = LB.render(LB.list[LB.i], LB.i);
  $(".bar", lb).innerHTML = `<span class="cap">${esc(r.caption || "")}</span>` +
    (r.actions || []).map((a, k) => a.href ? `<a href="${a.href}" class="${a.cls || ""}">${esc(a.label)}</a>` : `<button data-a="${k}" class="${a.cls || ""}">${esc(a.label)}</button>`).join("") +
    `<button class="close">Fechar ✕</button>`;
  $(".bar", lb).onclick = e => { const k = e.target.dataset.a; if (k != null) r.actions[k].run(); };
  const multi = LB.list.length > 1;
  $(".stage", lb).innerHTML = r.html + (r.note ? `<div class="note">${esc(r.note)}</div>` : "") +
    (multi ? `<button class="nav prev">‹</button><button class="nav next">›</button>` : "");
  const hi = $("img[data-hi]", lb);
  if (hi) { const im = new Image(); im.onload = () => { if (hi.isConnected) { hi.src = im.src; $(".note", lb)?.remove(); } }; im.src = hi.dataset.hi; }
}
function closeLb() { lb.classList.remove("on"); $(".stage", lb).innerHTML = ""; }
function step(d) { if (LB.list && LB.list[LB.i + d]) { LB.i += d; drawLb(); } }
lb.addEventListener("click", e => {
  if (e.target.classList.contains("close") || e.target.classList.contains("stage")) closeLb();
  if (e.target.classList.contains("prev")) step(-1);
  if (e.target.classList.contains("next")) step(1);
});
document.addEventListener("keydown", e => {
  if (!lb.classList.contains("on") || $("#dlg").open) return;
  if (e.key === "Escape") closeLb();
  if (e.key === "ArrowLeft") step(-1);
  if (e.key === "ArrowRight") step(1);
});

/* ---------- Fotos ---------- */
let photos = [], shown = [], rendered = 0, filter = "all", io = null, lastMonth = null, curTiles = null;
async function loadPhotos() {
  $("#grid").innerHTML = `<div class="empty">Carregando fotos…</div>`;
  photos = await api(base + "/photos");
  applyFilter();
  return true;
}
$("#photos .toolbar").onclick = e => {
  if (!e.target.dataset.f) return;
  filter = e.target.dataset.f;
  $$("#photos .chip").forEach(c => c.classList.toggle("on", c === e.target));
  applyFilter();
};
function applyFilter() {
  const f = {all: p => !p.hidden, photo: p => !p.video && !p.hidden, video: p => p.video && !p.hidden, fav: p => p.fav, hidden: p => p.hidden}[filter];
  shown = photos.filter(f);
  $("#pcount").textContent = `${num(shown.length)} itens`;
  $("#grid").innerHTML = shown.length ? "" : `<div class="empty">Nada aqui.</div>`;
  rendered = 0; lastMonth = null; renderMore();
}
function renderMore() {
  const grid = $("#grid"), end = Math.min(rendered + 300, shown.length);
  $(".sentinel", grid)?.remove();
  for (; rendered < end; rendered++) {
    const i = rendered, p = shown[i], m = fmtMonth(p.date);
    if (m !== lastMonth) {
      lastMonth = m;
      grid.insertAdjacentHTML("beforeend", `<div class="month">${esc(m)}</div><div class="tiles"></div>`);
      curTiles = grid.lastElementChild;
    }
    const t = document.createElement("div");
    t.className = "tile" + (p.has ? "" : " missing");
    t.title = `${p.name} · ${fmtDate(p.date)}${p.has ? "" : " · original não está no backup"}`;
    t.innerHTML = `<img loading="lazy" src="${base}/photo/${p.id}/thumb" alt="">` +
      (p.video ? `<span class="badge">▶ ${fmtDur(p.dur)}</span>` : "") + (p.fav ? `<span class="fav">♥</span>` : "");
    t.onclick = () => openLb(shown, i, renderPhoto);
    curTiles.appendChild(t);
  }
  if (rendered < shown.length) {
    const s = document.createElement("div"); s.className = "sentinel"; s.style.height = "1px";
    grid.appendChild(s);
    io?.disconnect();
    io = new IntersectionObserver(es => es[0].isIntersecting && renderMore(), {root: grid, rootMargin: "800px"});
    io.observe(s);
  }
}
function renderPhoto(p) {
  const orig = `${base}/photo/${p.id}/original`, thumb = `${base}/photo/${p.id}/thumb`, jpg = `${base}/photo/${p.id}/jpg`;
  const actions = [];
  let html, note = "";
  if (!p.has) { html = `<img src="${thumb}">`; note = "Original não está no backup (provavelmente só no iCloud) — mostrando a miniatura"; }
  else if (p.video) { html = `<video controls autoplay src="${orig}"></video>`; note = "Se o vídeo não tocar (formato HEVC), use “Baixar original”"; }
  else if (WEB_IMG.test(p.name)) html = `<img src="${orig}">`;
  else if (HEIC.test(p.name) && B.heic) { html = `<img src="${thumb}" data-hi="${jpg}">`; note = "Convertendo HEIC em alta resolução…"; }
  else { html = `<img src="${thumb}">`; note = "Prévia — instale pillow-heif para ver HEIC em alta resolução"; }
  if (p.has) actions.push({label: "Baixar original", href: orig + "?dl=1"});
  if (p.has && HEIC.test(p.name) && B.heic) actions.push({label: "Baixar JPG", href: jpg + "?dl=1"});
  return {html, note, actions, caption: `${p.name} · ${fmtDT(p.date)}${p.w ? ` · ${p.w}×${p.h}` : ""}`};
}

/* ---------- WhatsApp ---------- */
const FICON = {todas: "💬", entrada: "📥", importantes: "⭐", inuteis: "🧹", grupos: "👥", lixeira: "🗑", media: "📦"};
const CATS = [["all", "Todas"], ["foto", "Fotos"], ["video", "Vídeos"], ["audio", "Áudios"], ["doc", "Documentos"], ["figurinha", "Figurinhas"], ["outro", "Outros"]];
const CAT_ICON = {foto: "🖼", video: "🎥", audio: "🎤", doc: "📄", figurinha: "🙂", outro: "📎"};
function color(s) {
  let h = 0; for (const ch of s || "") h = (h * 31 + ch.charCodeAt(0)) % 360;
  return `hsl(${h} 55% ${matchMedia("(prefers-color-scheme: dark)").matches ? 68 : 38}%)`;
}

function makeWA(app) {
  const el = document.createElement("section");
  el.className = "view wa dyn";
  el.innerHTML = `
    <aside class="folders"></aside>
    <div class="chats">
      <select class="fselect"></select>
      <div class="searchrow">
        <input type="search" placeholder="Buscar conversas…">
        <div class="seg"><button data-m="chats" class="on">Conversas</button><button data-m="msgs">Texto nas mensagens</button></div>
      </div>
      <div class="listbar">
        <label title="Selecionar todas"><input type="checkbox" class="selall"></label>
        <span class="cnt"></span>
        <select class="sort">
          <option value="recent">Mais recentes</option><option value="msgs">Mais mensagens</option>
          <option value="size">Mídia mais pesada</option><option value="name">Nome (A–Z)</option>
        </select>
      </div>
      <div class="folderbar" hidden></div>
      <div class="bulk" hidden></div>
      <div class="chatlist"><div class="empty">Carregando… (a primeira vez pode levar alguns segundos)</div></div>
    </div>
    <div class="conv"><div class="empty">Selecione uma conversa</div></div>`;
  $("main").appendChild(el);
  const wb = `${base}/wa/${app}`;
  const S = {folders: [], chats: [], byId: {}, folder: "todas", sort: "recent", mode: "chats", sel: new Set(), cur: null, results: null};
  const conv = $(".conv", el), listEl = $(".chatlist", el), input = $(".searchrow input", el);

  const isFolder = f => S.folders.some(x => x.id === f);
  const fname = f => f === "todas" ? "Todas" : f === "lixeira" ? "Lixeira" : (S.folders.find(x => x.id === f) || {}).name || f;
  const inFolder = (c, f) => f === "lixeira" ? c.deleted : (c.deleted && !c.keep) ? false : f === "todas" || c.folder === f;
  const action = async (body, msg) => { try { const r = await post(`/api/wa/${app}/action`, body); if (msg) toast(msg); return r; } catch (e) { toast("Erro: " + e.message); throw e; } };

  async function reload() {
    try {
      const r = await api(wb + "/chats");
      S.folders = r.folders; S.chats = r.chats; S.byId = Object.fromEntries(r.chats.map(c => [c.id, c]));
      if (S.folder !== "todas" && S.folder !== "lixeira" && S.folder !== "media" && !isFolder(S.folder)) S.folder = "todas";
      for (const id of [...S.sel]) if (!S.byId[id]) S.sel.delete(id);
      drawFolders(); drawList();
      if (S.cur && V.chat) { V.chat = S.cur = S.byId[S.cur.id] || S.cur; drawHead(); }
    } catch (e) { listEl.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  }
  reload();

  /* pastas */
  function folderEntries() {
    return [{id: "todas", name: "Todas"}, ...S.folders, {id: "lixeira", name: "Lixeira"}]
      .map(f => ({...f, n: S.chats.filter(c => inFolder(c, f.id)).length}));
  }
  function drawFolders() {
    const fs = folderEntries();
    $(".folders", el).innerHTML = `<h4>Pastas</h4>` + fs.map(f => `
      <button class="fitem${S.folder === f.id ? " on" : ""}" data-f="${f.id}">
        <span>${FICON[f.id] || "📁"}</span><span class="fn">${esc(f.name)}</span>
        ${f.builtin === false ? `<span class="fx" data-x="ren" title="Renomear">✎</span><span class="fx" data-x="del" title="Apagar pasta">✕</span>` : ""}
        <span class="fc">${num(f.n)}</span>
      </button>`).join("") +
      `<button class="btn" data-x="new" style="margin:6px 8px">+ Nova pasta</button>
       <h4 style="margin-top:14px">Análise</h4>
       <button class="fitem${S.folder === "media" ? " on" : ""}" data-f="media"><span>📦</span><span class="fn">Todas as mídias</span></button>
       <p style="font-size:11.5px;color:var(--muted);margin:14px 8px">Arraste conversas para uma pasta. Excluir só oculta aqui — o backup original nunca é alterado.</p>`;
    $(".fselect", el).innerHTML = fs.map(f => `<option value="${f.id}" ${S.folder === f.id ? "selected" : ""}>${FICON[f.id] || "📁"} ${esc(f.name)} (${f.n})</option>`).join("") +
      `<option value="media" ${S.folder === "media" ? "selected" : ""}>📦 Todas as mídias</option>`;
  }
  $(".fselect", el).onchange = e => setFolder(e.target.value);
  $(".folders", el).addEventListener("click", async e => {
    const x = e.target.closest("[data-x]")?.dataset.x, item = e.target.closest(".fitem");
    if (x === "new") {
      const name = prompt("Nome da nova pasta:");
      if (name && name.trim()) { await action({op: "folder_create", name}); reload(); }
      return;
    }
    if (x === "ren") {
      const name = prompt("Novo nome:", fname(item.dataset.f));
      if (name && name.trim()) { await action({op: "folder_rename", id: item.dataset.f, name}); reload(); }
      return;
    }
    if (x === "del") {
      if (await ask({title: `Apagar a pasta “${fname(item.dataset.f)}”?`, text: "As conversas dela voltam para a pasta automática (Sem pasta, Inúteis ou Grupos). Nenhuma conversa é excluída.", ok: "Apagar pasta", danger: true})) {
        await action({op: "folder_delete", id: item.dataset.f}); reload();
      }
      return;
    }
    if (item) setFolder(item.dataset.f);
  });
  // arrastar e soltar conversas nas pastas
  $(".folders", el).addEventListener("dragover", e => { const f = e.target.closest(".fitem"); if (f && isFolder(f.dataset.f)) { e.preventDefault(); f.classList.add("drop"); } });
  $(".folders", el).addEventListener("dragleave", e => e.target.closest(".fitem")?.classList.remove("drop"));
  $(".folders", el).addEventListener("drop", async e => {
    const f = e.target.closest(".fitem"); if (!f) return;
    e.preventDefault(); f.classList.remove("drop");
    const ids = JSON.parse(e.dataTransfer.getData("text/x-chats") || "[]");
    if (ids.length) { await action({op: "move", chats: ids.map(id => S.byId[id].jid), folder: f.dataset.f}, `${ids.length} conversa(s) movida(s) para ${fname(f.dataset.f)}`); reload(); }
  });

  function setFolder(f) {
    S.folder = f; S.sel.clear(); drawFolders();
    if (f === "media") { el.classList.add("open"); openGlobalMedia(); }
    drawList();
  }

  /* lista de conversas */
  input.oninput = () => { if (S.mode === "chats") drawList(); else { clearTimeout(S.st); S.st = setTimeout(runSearch, 450); } };
  input.onkeydown = e => { if (e.key === "Enter" && S.mode === "msgs") runSearch(); };
  $(".seg", el).onclick = e => {
    const m = e.target.dataset.m; if (!m) return;
    S.mode = m; $$(".seg button", el).forEach(b => b.classList.toggle("on", b.dataset.m === m));
    input.placeholder = m === "chats" ? "Buscar conversas…" : "Buscar texto em todas as mensagens…";
    if (m === "msgs") runSearch(); else drawList();
    input.focus();
  };
  $(".sort", el).onchange = e => { S.sort = e.target.value; drawList(); };

  function visibleChats() {
    const q = input.value.trim().toLowerCase();
    const items = S.chats.filter(c => inFolder(c, S.folder === "media" ? "todas" : S.folder) && (!q || c.name.toLowerCase().includes(q) || c.jid.includes(q)));
    const cmp = {recent: (a, b) => (b.date || 0) - (a.date || 0), msgs: (a, b) => b.count - a.count,
                 size: (a, b) => b.media_bytes - a.media_bytes, name: (a, b) => a.name.localeCompare(b.name, "pt-BR")}[S.sort];
    return items.sort(cmp);
  }
  function avatar(c) {
    if (c.photo) return `<div class="av"><img loading="lazy" src="${wb}/avatar/${encodeURIComponent(c.jid)}" alt=""></div>`;
    const ini = c.group ? "👥" : esc((c.name.match(/[\p{L}\p{N}]/u) || ["?"])[0].toUpperCase());
    return `<div class="av" style="background:${color(c.name)}">${ini}</div>`;
  }
  function drawList() {
    if (S.mode === "msgs") return drawResults();
    const items = visibleChats();
    S.visible = items;
    $(".cnt", el).textContent = `${num(items.length)} conversas`;
    $(".selall", el).checked = items.length > 0 && items.every(c => S.sel.has(c.id));
    $(".listbar", el).style.display = "";
    listEl.innerHTML = items.map(c => `
      <div class="chat${S.cur && S.cur.id === c.id ? " on" : ""}${c.deleted && S.folder !== "lixeira" ? " gone" : ""}" data-id="${c.id}" draggable="true">
        <input type="checkbox" class="ck" ${S.sel.has(c.id) ? "checked" : ""}>
        ${avatar(c)}
        <div class="ci">
          <div class="r1"><span class="n">${esc(c.name)}</span><span class="d">${fmtDate(c.date)}</span></div>
          <div class="l">${esc(c.last)}</div>
          <div class="meta"><span>${num(c.count)} msg</span>${c.media_n ? `<span>${num(c.media_n)} mídias · ${fmtSize(c.media_bytes)}</span>` : ""}${c.fav ? `<span>★ ${c.fav}</span>` : ""}
            ${S.folder === "todas" || S.folder === "lixeira" ? `<span class="tag">${esc(fname(c.folder))}</span>` : ""}${c.keep ? `<span class="tag">só favoritas</span>` : ""}</div>
        </div>
      </div>`).join("") || `<div class="empty">Nenhuma conversa aqui</div>`;
    drawBars();
  }
  function drawBars() {
    const fb = $(".folderbar", el), bulk = $(".bulk", el);
    const inF = S.visible || [];
    if (S.folder === "lixeira" && inF.length) {
      fb.hidden = false;
      fb.innerHTML = `<span style="flex:1;color:var(--muted)">Conversas excluídas ficam aqui e podem ser restauradas.</span><button class="btn" data-b="restoreall">Restaurar todas</button>`;
    } else if (S.folder !== "todas" && S.folder !== "media" && S.folder !== "lixeira" && inF.length) {
      fb.hidden = false;
      fb.innerHTML = `<span style="flex:1;color:var(--muted)">${esc(fname(S.folder))}: ${num(inF.length)} conversas</span><button class="btn danger" data-b="delall">Excluir todas desta pasta…</button>`;
    } else fb.hidden = true;
    const n = S.sel.size;
    bulk.hidden = !n;
    if (n) bulk.innerHTML = `<b>${n} selecionada(s)</b>
      <select data-b="move"><option value="">Mover para…</option>${S.folders.map(f => `<option value="${f.id}">${esc(f.name)}</option>`).join("")}<option value="auto">↺ Pasta automática</option></select>
      ${S.folder === "lixeira" ? `<button class="btn" data-b="restore">Restaurar</button>` : `<button class="btn danger" data-b="del">Excluir…</button>`}
      <button class="btn" data-b="clear">Limpar</button>`;
  }
  async function deleteChats(chats, label) {
    const favs = chats.reduce((s, c) => s + c.fav, 0);
    const r = await ask({title: `Excluir ${label}?`, text: "As conversas vão para a Lixeira (podem ser restauradas). O backup original não é alterado.",
      check: favs ? {label: `Manter as mensagens favoritas (${num(favs)} ★)`, value: true} : null, ok: "Excluir", danger: true});
    if (!r) return false;
    await action({op: "delete_chats", chats: chats.map(c => c.jid), keep_starred: !!r.checked}, `${chats.length} conversa(s) movida(s) para a Lixeira`);
    S.sel.clear(); await reload(); return true;
  }
  el.addEventListener("click", async e => {
    const b = e.target.closest("[data-b]")?.dataset.b; if (!b || e.target.tagName === "SELECT") return;
    const sel = [...S.sel].map(id => S.byId[id]);
    if (b === "clear") { S.sel.clear(); drawList(); }
    if (b === "del") await deleteChats(sel, `${sel.length} conversa(s)`);
    if (b === "restore") { await action({op: "restore_chats", chats: sel.map(c => c.jid)}, "Restauradas"); S.sel.clear(); reload(); }
    if (b === "delall") await deleteChats(S.visible.filter(c => !c.deleted), `todas as ${num(S.visible.length)} conversas de “${fname(S.folder)}”`);
    if (b === "restoreall") {
      if (await ask({title: "Restaurar todas as conversas da Lixeira?", ok: "Restaurar"})) { await action({op: "restore_chats", chats: S.visible.map(c => c.jid)}, "Restauradas"); reload(); }
    }
  });
  el.addEventListener("change", async e => {
    if (e.target.dataset.b === "move" && e.target.value) {
      const sel = [...S.sel].map(id => S.byId[id]);
      await action({op: "move", chats: sel.map(c => c.jid), folder: e.target.value}, `${sel.length} conversa(s) movida(s)`);
      S.sel.clear(); reload();
    }
  });
  $(".selall", el).onchange = e => { for (const c of S.visible || []) e.target.checked ? S.sel.add(c.id) : S.sel.delete(c.id); drawList(); };
  listEl.addEventListener("click", e => {
    const d = e.target.closest(".chat"), r = e.target.closest(".result");
    if (r) return openChat(S.byId[+r.dataset.chat], {jump: +r.dataset.msg});
    if (!d) return;
    const id = +d.dataset.id;
    if (e.target.classList.contains("ck")) { e.target.checked ? S.sel.add(id) : S.sel.delete(id); $(".selall", el).checked = false; drawBars(); return; }
    openChat(S.byId[id]);
  });
  listEl.addEventListener("dragstart", e => {
    const d = e.target.closest(".chat"); if (!d) return;
    const id = +d.dataset.id;
    const ids = S.sel.has(id) ? [...S.sel] : [id];
    e.dataTransfer.setData("text/x-chats", JSON.stringify(ids));
  });

  /* busca de texto em todas as mensagens */
  async function runSearch() {
    const q = input.value.trim();
    $(".listbar", el).style.display = "none"; $(".folderbar", el).hidden = true; $(".bulk", el).hidden = true;
    if (q.length < 2) { S.results = null; listEl.innerHTML = `<div class="empty">Digite ao menos 2 letras. A busca ignora acentos e maiúsculas.</div>`; return; }
    listEl.innerHTML = `<div class="empty">Buscando…</div>`;
    try { S.results = {q, ...(await api(`${wb}/search?q=${encodeURIComponent(q)}`))}; } catch (e) { listEl.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    if (input.value.trim() === q) drawResults();
  }
  function drawResults() {
    const R = S.results;
    if (!R) { listEl.innerHTML = `<div class="empty">Digite e pressione Enter para buscar nas mensagens.</div>`; return; }
    listEl.innerHTML = (R.items.length ? `<div class="sys" style="padding:8px">${num(R.items.length)}${R.more ? "+" : ""} resultado(s)</div>` : "") +
      (R.items.map(resultHtml).join("") || `<div class="empty">Nada encontrado para “${esc(R.q)}”</div>`);
  }
  const resultHtml = r => `<div class="result" data-chat="${r.chat}" data-msg="${r.msg}">
      <div class="r1"><b>${esc(r.chat_name)}</b><span>${fmtDate(r.date)}</span></div>
      <div class="s">${r.me ? "Você: " : ""}${esc(r.pre)}<mark>${esc(r.hit)}</mark>${esc(r.post)}</div></div>`;

  /* conversa */
  const V = {chat: null, view: "msgs", onlyFav: false, showDel: false};
  const msgMode = () => V.onlyFav ? "starred" : (V.chat.deleted && (S.folder === "lixeira" || !V.chat.keep) ? "all" : "normal");

  function openChat(c, opts = {}) {
    if (!c) return;
    S.cur = c; el.classList.add("open");
    $$(".chat", listEl).forEach(d => d.classList.toggle("on", +d.dataset.id === c.id));
    if (S.folder === "media") { S.folder = "todas"; drawFolders(); drawList(); }
    Object.assign(V, {chat: c, view: opts.media ? "media" : "msgs", onlyFav: false, showDel: false});
    conv.innerHTML = `<div class="convhead"></div><div class="cbanner"></div><div class="cbody" style="flex:1;min-height:0;display:flex;flex-direction:column"></div>`;
    drawHead(); drawBody(opts.jump);
  }
  function drawHead() {
    const c = V.chat; if (!c || !$(".convhead", conv)) return;
    c.fav = (S.byId[c.id] || c).fav;
    $(".convhead", conv).innerHTML = `
      <button class="btn back" style="display:none">‹</button>
      ${avatar(c)}
      <div class="tt"><div class="t">${esc(c.name)}</div><div class="s">${num(c.count)} mensagens · ${num(c.media_n)} mídias (${fmtSize(c.media_bytes)}) · ★ ${c.fav}</div></div>
      <div class="tools">
        <div class="seg" style="min-width:170px"><button data-v="msgs" class="${V.view === "msgs" ? "on" : ""}">💬 Mensagens</button><button data-v="media" class="${V.view === "media" ? "on" : ""}">🖼 Mídias</button></div>
        <button class="btn" data-h="search" title="Buscar nesta conversa">🔎</button>
        <select data-h="folder" title="Pasta">${S.folders.map(f => `<option value="${f.id}" ${c.folder === f.id ? "selected" : ""}>📁 ${esc(f.name)}</option>`).join("")}<option value="auto">↺ Pasta automática</option></select>
        <details class="menu"><summary class="btn">⋯</summary><div class="pop">
          <button data-h="fav">${V.onlyFav ? "✓ " : ""}★ Mostrar só favoritas</button>
          <button data-h="showdel">${V.showDel ? "✓ " : ""}Mostrar mensagens excluídas</button>
          <a href="${wb}/chat/${c.id}/export.txt?mode=${msgMode()}">Exportar .txt (o que está visível)</a>
          ${c.deleted ? `<button data-h="restore">↺ Restaurar conversa</button>` : `<button data-h="del" class="danger">🗑 Excluir conversa…</button>`}
        </div></details>
      </div>`;
    $(".cbanner", conv).innerHTML = !c.deleted ? "" : c.keep && S.folder !== "lixeira"
      ? `<div class="banner">Conversa excluída — mostrando só as mensagens favoritas. <button class="btn" data-h="restore">Restaurar conversa completa</button></div>`
      : `<div class="banner">Esta conversa está na Lixeira. <button class="btn" data-h="restore">Restaurar</button></div>`;
  }
  conv.addEventListener("click", async e => {
    const v = e.target.closest("[data-v]")?.dataset.v, h = e.target.closest("[data-h]")?.dataset.h;
    if (e.target.classList.contains("back")) { el.classList.remove("open"); return; }
    if (v && v !== V.view) { V.view = v; drawHead(); drawBody(); }
    if (!h || h === "folder") return;
    e.target.closest("details")?.removeAttribute("open");
    if (h === "fav") { V.onlyFav = !V.onlyFav; V.view = "msgs"; drawHead(); drawBody(); }
    if (h === "showdel") { V.showDel = !V.showDel; V.view = "msgs"; drawHead(); drawBody(); }
    if (h === "search") toggleInSearch();
    if (h === "del") { if (await deleteChats([V.chat], `a conversa “${V.chat.name}”`)) { V.chat = S.byId[V.chat.id]; drawHead(); drawBody(); } }
    if (h === "restore") { await action({op: "restore_chats", chats: [V.chat.jid]}, "Conversa restaurada"); await reload(); V.chat = S.byId[V.chat.id]; drawHead(); drawBody(); }
  });
  conv.addEventListener("change", async e => {
    if (e.target.dataset.h === "folder") {
      await action({op: "move", chats: [V.chat.jid], folder: e.target.value}, `Movida para ${e.target.value === "auto" ? "a pasta automática" : fname(e.target.value)}`);
      await reload();
    }
  });
  function toggleInSearch() {
    const ex = $(".insearch", conv);
    if (ex) { ex.remove(); $(".inresults", conv)?.remove(); return; }
    $(".cbanner", conv).insertAdjacentHTML("afterend", `<div class="insearch"><input type="search" placeholder="Buscar nesta conversa e pressionar Enter…"><button class="btn">Fechar</button></div>`);
    const box = $(".insearch", conv), inp = $("input", box);
    inp.focus();
    $("button", box).onclick = toggleInSearch;
    inp.onkeydown = async e => {
      if (e.key === "Escape") return toggleInSearch();
      if (e.key !== "Enter") return;
      $(".inresults", conv)?.remove();
      const q = inp.value.trim(); if (q.length < 2) return;
      const R = await api(`${wb}/search?q=${encodeURIComponent(q)}&chat=${V.chat.id}`);
      box.insertAdjacentHTML("afterend", `<div class="inresults">${R.items.map(resultHtml).join("") || `<div class="empty">Nada encontrado</div>`}</div>`);
      const res = $(".inresults", conv);
      res.style.top = (box.offsetTop + box.offsetHeight) + "px";
      res.onclick = ev => { const r = ev.target.closest(".result"); if (!r) return; res.remove(); V.view = "msgs"; V.onlyFav = false; drawHead(); drawBody(+r.dataset.msg); };
    };
  }
  function drawBody(jump) {
    const body = $(".cbody", conv);
    if (V.view === "media") return mediaPane(body, V.chat);
    body.innerHTML = `<div class="msgs"></div>`;
    msgPane($(".msgs", body), V.chat, jump);
  }

  function msgPane(box, c, jump) {
    const P = {msgs: [], older: 0, newer: 0, doneOld: false, doneNew: true, loading: false};
    const mode = msgMode(), showdel = V.showDel ? 1 : 0;
    const q = (dir, cur) => api(`${wb}/chat/${c.id}/messages?dir=${dir}&cursor=${cur}&limit=150&mode=${mode}&showdel=${showdel}`);
    const dayOf = m => m.date ? new Date(m.date * 1000).toDateString() : "";
    function range(list, prevDay) {
      let html = "", day = prevDay;
      for (const m of list) {
        const d = dayOf(m);
        if (d !== day) { day = d; html += `<div class="day" data-d="${d}">${m.date ? fmtDay(m.date) : ""}</div>`; }
        html += renderMsg(m, c.group);
      }
      return html;
    }
    function edges() {
      $(".edge-top", box)?.remove(); $(".edge-bot", box)?.remove();
      box.insertAdjacentHTML("afterbegin", `<div class="sys edge-top">${P.doneOld ? (P.msgs.length ? "Início da conversa" : "") : "Carregando…"}</div>`);
      if (!P.doneNew) box.insertAdjacentHTML("beforeend", `<div class="sys edge-bot">Carregando mais recentes…</div>`);
      if (!P.msgs.length && P.doneOld && P.doneNew) box.innerHTML = `<div class="empty">${V.onlyFav ? "Nenhuma mensagem favorita nesta conversa" : "Nenhuma mensagem visível"}</div>`;
    }
    function prepend(items) {
      $(".edge-top", box)?.remove();
      const first = $(".day", box);
      if (first && items.length && first.dataset.d === dayOf(items[items.length - 1])) first.remove();
      box.insertAdjacentHTML("afterbegin", range(items, null));
    }
    function append(items) {
      $(".edge-bot", box)?.remove();
      box.insertAdjacentHTML("beforeend", range(items, P.msgs.length ? dayOf(P.msgs[P.msgs.length - 1]) : null));
    }
    async function loadOlder() {
      if (P.loading || P.doneOld) return;
      P.loading = true;
      const r = await q("older", P.older);
      if (!box.isConnected) return;
      const fromBottom = box.scrollHeight - box.scrollTop;
      prepend(r.items);
      P.msgs = [...r.items, ...P.msgs]; P.older = r.cursor; P.doneOld = r.done;
      edges();
      box.scrollTop = box.scrollHeight - fromBottom;
      P.loading = false; fill();
    }
    async function loadNewer() {
      if (P.loading || P.doneNew) return;
      P.loading = true;
      const r = await q("newer", P.newer);
      if (!box.isConnected) return;
      append(r.items);
      P.msgs = [...P.msgs, ...r.items]; P.newer = r.cursor; P.doneNew = r.done;
      edges(); P.loading = false; fill();
    }
    function fill() { if (box.scrollHeight <= box.clientHeight + 40) { if (!P.doneOld) loadOlder(); else if (!P.doneNew) loadNewer(); } }
    box.onscroll = () => {
      if (box.scrollTop < 400) loadOlder();
      else if (box.scrollHeight - box.scrollTop - box.clientHeight < 400) loadNewer();
    };
    box.onclick = async e => {
      const img = e.target.closest("img.media");
      if (img) return openLb([img.dataset.full], 0, src => ({html: `<img src="${src}">`, caption: img.dataset.cap, actions: [{label: "Baixar", href: src + "?dl=1"}]}));
      const a = e.target.closest("[data-act]"); if (!a) return;
      const node = a.closest(".msg"), m = P.msgs.find(x => x.key === node.dataset.key);
      if (a.dataset.act === "star") {
        await action({op: "star", key: m.key, value: !m.fav}, m.fav ? "Removida das favoritas" : "Adicionada às favoritas ★");
        m.fav = !m.fav;
        if (mode === "starred" && !m.fav) node.remove(); else node.outerHTML = renderMsg(m, c.group);
        reload();
      }
      if (a.dataset.act === "del") {
        await action({op: "delete_msgs", keys: [m.key]}, "Mensagem excluída");
        if (showdel) { m.deleted = true; node.outerHTML = renderMsg(m, c.group); } else { node.remove(); P.msgs = P.msgs.filter(x => x !== m); }
      }
      if (a.dataset.act === "res") {
        await action({op: "restore_msgs", keys: [m.key]}, "Mensagem restaurada");
        m.deleted = false; node.outerHTML = renderMsg(m, c.group);
      }
    };
    (async () => {
      P.loading = true;
      if (jump) {
        const {pos} = await api(`${wb}/chat/${c.id}/position?msg=${jump}`);
        const [o, n] = await Promise.all([q("older", pos), pos > 0 ? q("newer", pos) : null]);
        P.msgs = [...o.items, ...(n ? n.items : [])]; P.older = o.cursor; P.doneOld = o.done;
        P.newer = n ? n.cursor : 0; P.doneNew = !n || n.done;
        box.innerHTML = range(P.msgs, null); edges();
        const t = $(`.msg[data-id="${jump}"]`, box);
        if (t) { t.scrollIntoView({block: "center"}); t.classList.add("hl"); }
      } else {
        const o = await q("older", 0);
        P.msgs = o.items; P.older = o.cursor; P.doneOld = o.done;
        box.innerHTML = range(P.msgs, null); edges();
        box.scrollTop = box.scrollHeight;
      }
      P.loading = false; fill();
    })().catch(e => box.innerHTML = `<div class="empty">${esc(e.message)}</div>`);
  }

  function renderMsg(m, group) {
    const md = m.media, t = m.type;
    if ((t === 6 || t === 10) && !m.fav) return m.text ? `<div class="sys">${esc(m.text)}</div>` : "";
    let body = "";
    if (md) {
      const full = `${wb}/media/${md.id}`, thumb = `${full}?thumb=1`;
      const cap = `${m.sender} · ${fmtDT(m.date)}`;
      const once = {38: "📷 Foto", 39: "🎥 Vídeo", 53: "🎤 Áudio"}[t];
      const miss = once ? `<div class="miss">${once} de visualização única (o WhatsApp não guarda)</div>`
        : `<div class="miss">${{1: "📷 Foto", 2: "🎥 Vídeo", 3: "🎤 Áudio", 8: "📄 Documento", 11: "GIF", 15: "Figurinha"}[t] || "📎 Mídia"} não está no backup (não foi baixada no iPhone)</div>`;
      if (once) body = miss;
      else if (t === 1) body = md.full || md.thumb ? `<img class="media" loading="lazy" src="${md.thumb ? thumb : full}" data-full="${md.full ? full : thumb}" data-cap="${esc(cap)}">` : miss;
      else if (t === 2 || t === 11) body = md.full ? `<video controls preload="none" ${md.thumb ? `poster="${thumb}"` : ""} src="${full}" ${t === 11 ? "loop muted" : ""}></video>` : miss;
      else if (t === 3) body = md.full ? `<audio controls preload="none" src="${full}"></audio>` : miss;
      else if (t === 15) body = md.full ? `<img class="media sticker" loading="lazy" src="${full}" data-full="${full}" data-cap="${esc(cap)}">` : miss;
      else if (t === 5 && md.lat) body = `<a class="doc" target="_blank" rel="noopener" href="https://www.openstreetmap.org/?mlat=${md.lat}&mlon=${md.lon}#map=16/${md.lat}/${md.lon}">📍 Localização ${md.lat.toFixed(5)}, ${md.lon.toFixed(5)}</a>`;
      else if (t === 4) body = `<span class="doc">👤 ${esc(md.vcard || "Contato")}</span>`;
      else if (md.full) body = `<a class="doc" href="${full}?dl=1">📄 ${esc(md.title || md.name || "Arquivo")} <small>${fmtSize(md.size)}</small></a>`;
      else if (t === 8) body = miss;
      if (md.full && md.size > 0 && [1, 2, 3].includes(t)) body += `<small style="color:var(--muted)">${fmtSize(md.size)}</small>`;
      if (md.title && t !== 8 && t !== 7 && !m.text) body += `<div>${esc(md.title)}</div>`;
    }
    if (m.text) body += (body ? "\n" : "") + esc(m.text);
    if (!body) body = `<span class="miss">${t === 14 ? "🚫 Mensagem apagada" : "Mensagem sem conteúdo"}</span>`;
    const who = group && !m.me ? `<div class="who" style="color:${color(m.sender)}">${esc(m.sender)}</div>` : "";
    const acts = m.deleted ? `<button data-act="res" title="Restaurar">↺ Restaurar</button>`
      : `<button data-act="star" title="${m.fav ? "Remover das favoritas" : "Favoritar"}">${m.fav ? "★" : "☆"}</button><button data-act="del" title="Excluir mensagem">🗑</button>`;
    return `<div class="msg${m.me ? " me" : ""}${m.deleted ? " del" : ""}" data-id="${m.id}" data-key="${esc(m.key)}"><span class="acts">${acts}</span>${who}${body}<span class="time">${m.fav ? `<span class="st">★</span> ` : ""}${fmtTime(m.date)}</span></div>`;
  }

  /* mídias (de uma conversa ou de todas) */
  function openGlobalMedia() {
    S.cur = null; V.chat = null;
    $$(".chat", listEl).forEach(d => d.classList.remove("on"));
    conv.innerHTML = `<div class="convhead"><button class="btn back" style="display:none">‹</button><div class="tt"><div class="t">📦 Todas as mídias</div><div class="s">Mídias de todas as conversas que não estão na Lixeira</div></div></div><div class="cbody" style="flex:1;min-height:0;display:flex;flex-direction:column"></div>`;
    mediaPane($(".cbody", conv), null);
  }
  function mediaPane(body, chat) {
    const M = {cat: "all", sort: chat ? "date" : "size", items: [], total: 0, bytes: 0, loading: false, sel: new Set()};
    const mode = chat ? msgMode() : "normal";
    body.innerHTML = `<div class="mpane">
      <div class="toolbar"><span class="mcats" style="display:flex;gap:6px;flex-wrap:wrap"></span>
        <select class="msort"><option value="size">Maiores primeiro</option><option value="size_asc">Menores primeiro</option><option value="date">Mais recentes</option><option value="date_asc">Mais antigas</option></select>
        <span class="count"></span></div>
      <div class="bulk mbulk" hidden></div>
      <div class="mgrid"><div class="tiles"></div><div class="sentinel" style="height:1px"></div></div></div>`;
    const grid = $(".tiles", body), sc = $(".mgrid", body);
    $(".msort", body).value = M.sort;
    $(".msort", body).onchange = e => { M.sort = e.target.value; reset(); };
    $(".mcats", body).onclick = e => { const k = e.target.closest("[data-c]")?.dataset.c; if (k) { M.cat = k; reset(); } };
    function reset() { M.items = []; M.sel.clear(); grid.innerHTML = ""; M.done = false; load(); }
    async function load() {
      if (M.loading || M.done) return;
      M.loading = true;
      const r = await api(`${wb}/media?chat=${chat ? chat.id : ""}&cat=${M.cat}&sort=${M.sort}&offset=${M.items.length}&limit=120&mode=${mode}`);
      M.total = r.total; M.bytes = r.bytes; M.cats = r.cats;
      const start = M.items.length;
      M.items.push(...r.items);
      M.done = M.items.length >= r.total;
      grid.insertAdjacentHTML("beforeend", r.items.map((it, k) => tile(it, start + k)).join(""));
      drawTop();
      if (!M.items.length) grid.innerHTML = `<div class="empty" style="grid-column:1/-1">Nenhuma mídia${M.cat !== "all" ? " deste tipo" : ""}</div>`;
      M.loading = false;
      if (sc.scrollHeight <= sc.clientHeight + 200 && !M.done) load();
    }
    function drawTop() {
      const all = Object.values(M.cats || {}).reduce((a, b) => a + b, 0);
      $(".mcats", body).innerHTML = CATS.filter(([k]) => k === "all" || (M.cats || {})[k])
        .map(([k, l]) => `<button class="chip${M.cat === k ? " on" : ""}" data-c="${k}">${l} <small>${num(k === "all" ? all : M.cats[k])}</small></button>`).join("");
      $(".count", body).textContent = `${num(M.total)} itens · ${fmtSize(M.bytes)}`;
      const mb = $(".mbulk", body), n = M.sel.size;
      mb.hidden = !n;
      if (n) {
        const sz = [...M.sel].reduce((s, i) => s + M.items[i].size, 0);
        mb.innerHTML = `<b>${n} selecionada(s) · ${fmtSize(sz)}</b><button class="btn danger" data-mb="del">Excluir mensagens…</button><button class="btn" data-mb="clear">Limpar</button>`;
      }
      body.querySelector(".mpane").classList.toggle("sel-mode", n > 0);
    }
    function tile(it, i) {
      const full = `${wb}/media/${it.id}`;
      let inner;
      if (it.thumb) inner = `<img loading="lazy" src="${full}?thumb=1" alt="">`;
      else if (it.cat === "foto" || it.cat === "figurinha") inner = `<img loading="lazy" src="${full}" alt="">`;
      else inner = `<div class="icon">${CAT_ICON[it.cat]}<small>${esc(it.cat === "doc" ? it.title || it.name : fmtDT(it.date))}</small></div>`;
      return `<div class="tile${M.sel.has(i) ? " sel" : ""}" data-i="${i}" title="${esc(it.name)} · ${fmtDT(it.date)}">${inner}
        ${chat ? "" : `<div class="who">${esc(it.chat_name)}</div>`}
        <input type="checkbox" class="ck" ${M.sel.has(i) ? "checked" : ""}>
        ${it.fav ? `<span class="fav">★</span>` : ""}
        <span class="badge">${it.cat === "video" || it.cat === "audio" ? (it.dur ? fmtDur(it.dur) + " · " : "") : ""}${fmtSize(it.size)}</span></div>`;
    }
    const io = new IntersectionObserver(es => es[0].isIntersecting && load(), {root: sc, rootMargin: "600px"});
    io.observe($(".sentinel", body));
    grid.onclick = e => {
      const t = e.target.closest(".tile"); if (!t) return;
      const i = +t.dataset.i;
      if (e.target.classList.contains("ck")) { e.target.checked ? M.sel.add(i) : M.sel.delete(i); t.classList.toggle("sel", e.target.checked); drawTop(); return; }
      openLb(M.items, i, renderMedia);
    };
    body.onclick = async e => {
      const b = e.target.dataset.mb; if (!b) return;
      if (b === "clear") { M.sel.clear(); $$(".tile", grid).forEach(t => { t.classList.remove("sel"); $(".ck", t).checked = false; }); drawTop(); }
      if (b === "del") {
        const items = [...M.sel].map(i => M.items[i]);
        if (await ask({title: `Excluir ${items.length} mensagem(ns) com mídia?`, text: "Elas ficam ocultas aqui e podem ser vistas com “Mostrar mensagens excluídas” na conversa. O backup original não é alterado.", ok: "Excluir", danger: true})) {
          await action({op: "delete_msgs", keys: items.map(x => x.key)}, `${items.length} mensagem(ns) excluída(s)`);
          reset(); reload();
        }
      }
    };
    function renderMedia(it) {
      const full = `${wb}/media/${it.id}`;
      let html;
      if (it.cat === "foto" || it.cat === "figurinha") html = `<img src="${full}">`;
      else if (it.cat === "video") html = `<video controls autoplay src="${full}"></video>`;
      else if (it.cat === "audio") html = `<div class="big">🎤<div><audio controls autoplay src="${full}"></audio></div></div>`;
      else html = `<div class="big">${CAT_ICON[it.cat]}<div>${esc(it.title || it.name)}</div></div>`;
      return {html, caption: `${it.chat_name} · ${fmtDT(it.date)} · ${fmtSize(it.size)}`, actions: [
        {label: "Baixar", href: full + "?dl=1"},
        {label: "Ir para a mensagem", run: () => { closeLb(); openChat(S.byId[it.chat], {jump: it.msg}); }},
        {label: "Excluir mensagem", cls: "danger", run: async () => {
          if (!await ask({title: "Excluir esta mensagem?", text: "Fica oculta aqui; o backup original não é alterado.", ok: "Excluir", danger: true})) return;
          await action({op: "delete_msgs", keys: [it.key]}, "Mensagem excluída");
          closeLb(); reset(); reload();
        }},
      ]};
    }
    load().catch(e => grid.innerHTML = `<div class="empty">${esc(e.message)}</div>`);
  }
  return {el};
}

/* ---------- Utilidades das abas novas ---------- */
function makeView(cls, html) {
  const el = document.createElement("section");
  el.className = "view dyn split " + cls;
  el.innerHTML = html;
  $("main").appendChild(el);
  return el;
}
const initials = name => esc((String(name || "?").match(/[\p{L}\p{N}]/u) || ["?"])[0].toUpperCase());
function imgLightbox(container) {
  container.addEventListener("click", e => {
    const im = e.target.closest("img[data-full]");
    if (im) openLb([im.dataset.full], 0, src => ({html: `<img src="${src}">`, actions: [{label: "Baixar", href: src + "&dl=1"}]}));
  });
}

/* ---------- Notas ---------- */
function makeNotes() {
  const el = makeView("notes-view", `
    <div class="col fcol folders" style="width:210px"></div>
    <div class="col" style="width:340px">
      <select class="fselect"></select>
      <div class="searchrow"><input type="search" placeholder="Buscar no texto das notas…"></div>
      <div class="listbar"><span class="cnt"></span>
        <select class="sort"><option value="mod">Editadas recentemente</option><option value="created">Criadas recentemente</option><option value="title">Título (A–Z)</option></select></div>
      <div class="list"><div class="empty">Carregando notas…</div></div>
    </div>
    <div class="detail"><div class="empty">Selecione uma nota</div></div>`);
  const S = {folders: [], notes: [], folder: "all", hits: null, cur: null, sort: "mod"};
  const list = $(".list", el), det = $(".detail", el), input = $(".searchrow input", el);
  imgLightbox(det);
  api(base + "/notes").then(r => { S.folders = r.folders; S.notes = r.notes; drawFolders(); drawList(); })
    .catch(e => list.innerHTML = `<div class="empty">${esc(e.message)}</div>`);

  const delCount = () => S.notes.filter(n => n.deleted).length;
  function folderList() {
    const byParent = {};
    S.folders.forEach(f => (byParent[f.parent || 0] = byParent[f.parent || 0] || []).push(f));
    const out = [{id: "all", name: "Todas as notas", icon: "🗒", n: S.notes.filter(n => !n.deleted).length, depth: 0}];
    const walk = (p, depth) => (byParent[p] || []).sort((a, b) => (a.type - b.type) || a.name.localeCompare(b.name, "pt-BR"))
      .forEach(f => { if (f.type !== 1) { out.push({id: f.id, name: f.name, icon: "📁", n: f.count, depth}); walk(f.id, depth + 1); } });
    walk(0, 0);
    S.folders.filter(f => f.type === 1).forEach(f => out.push({id: f.id, name: f.name, icon: "🗑", n: f.count, depth: 0}));
    out.push({id: "deleted", name: "Excluídas (recuperáveis)", icon: "♻️", n: delCount(), depth: 0});
    return out;
  }
  function drawFolders() {
    const fs = folderList();
    $(".folders", el).innerHTML = `<h4>Pastas</h4>` + fs.map(f => `
      <button class="fitem${S.folder == f.id ? " on" : ""}" data-f="${f.id}" style="padding-left:${8 + f.depth * 16}px">
        <span>${f.icon}</span><span class="fn">${esc(f.name)}</span><span class="fc">${num(f.n)}</span></button>`).join("") +
      `<p style="font-size:11.5px;color:var(--muted);margin:14px 8px">“Excluídas (recuperáveis)” são notas que você apagou, mas cujo conteúdo ainda está guardado no backup.</p>`;
    $(".fselect", el).innerHTML = fs.map(f => `<option value="${f.id}" ${S.folder == f.id ? "selected" : ""}>${"  ".repeat(f.depth)}${f.icon} ${esc(f.name)} (${f.n})</option>`).join("");
  }
  const setFolder = f => { S.folder = f; drawFolders(); drawList(); };
  $(".folders", el).onclick = e => { const b = e.target.closest(".fitem"); if (b) setFolder(b.dataset.f); };
  $(".fselect", el).onchange = e => setFolder(e.target.value);
  $(".sort", el).onchange = e => { S.sort = e.target.value; drawList(); };
  let st;
  input.oninput = () => { clearTimeout(st); st = setTimeout(async () => {
    const q = input.value.trim();
    S.hits = q.length >= 2 ? new Set(await api(`${base}/notes/search?q=${encodeURIComponent(q)}`)) : null;
    drawList();
  }, 300); };
  function drawList() {
    let items = S.notes.filter(n => S.folder === "all" ? !n.deleted : S.folder === "deleted" ? n.deleted : (!n.deleted && n.folder == S.folder));
    if (S.hits) items = items.filter(n => S.hits.has(n.id));
    const cmp = {mod: (a, b) => (b.modified || 0) - (a.modified || 0), created: (a, b) => (b.created || 0) - (a.created || 0),
                 title: (a, b) => a.title.localeCompare(b.title, "pt-BR")}[S.sort];
    items.sort(cmp);
    $(".cnt", el).textContent = `${num(items.length)} notas`;
    list.innerHTML = items.map(n => `
      <div class="item${S.cur === n.id ? " on" : ""}" data-id="${n.id}">
        <div class="ci" style="flex:1;min-width:0">
          <div class="r1"><span class="n" style="font-weight:600">${n.locked ? "🔒 " : ""}${esc(n.title)}</span><span class="d">${fmtDate(n.modified)}</span></div>
          <div class="l">${esc(n.snippet || "")}</div>
          ${n.atts ? `<div class="meta"><span>📎 ${n.atts} anexo(s)</span></div>` : ""}
        </div></div>`).join("") || `<div class="empty">Nenhuma nota${S.hits ? " encontrada" : ""}</div>`;
  }
  list.onclick = e => { const it = e.target.closest(".item"); if (it) openNote(+it.dataset.id); };
  async function openNote(id) {
    S.cur = id; $$(".item", list).forEach(i => i.classList.toggle("on", +i.dataset.id === id));
    el.classList.add("open");
    det.innerHTML = `<div class="empty">Carregando…</div>`;
    try {
      const n = await api(`${base}/notes/${id}`);
      det.innerHTML = `<div class="inner">
        <div class="dhead"><button class="btn back2" style="display:none">‹</button><h2>${esc(n.title)}</h2>
          <a class="btn" href="${base}/notes/${id}/export.txt">Exportar .txt</a></div>
        <div class="meta-line">${n.deleted ? `<span class="badge2">♻️ excluída — recuperada do backup</span> · ` : ""}${n.folder_name ? `📁 ${esc(n.folder_name)} · ` : ""}Criada em ${fmtDT(n.created)} · Editada em ${fmtDT(n.modified)}</div>
        <div class="notebody">${n.html}</div></div>`;
      $(".back2", det).onclick = () => el.classList.remove("open");
    } catch (e) { det.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  }
  return {el};
}

/* ---------- Contatos ---------- */
function makeContacts() {
  const el = makeView("contacts-view", `
    <div class="col" style="width:360px">
      <div class="searchrow"><input type="search" placeholder="Buscar nome, telefone ou e-mail…"></div>
      <div class="listbar"><span class="cnt"></span><a class="btn" style="margin-left:auto" href="${base}/contacts.vcf">⬇ Exportar todos (.vcf)</a></div>
      <div class="list"><div class="empty">Carregando contatos…</div></div>
    </div>
    <div class="detail"><div class="empty">Selecione um contato</div></div>`);
  let all = [], cur = null;
  const list = $(".list", el), det = $(".detail", el), input = $(".searchrow input", el);
  const digits = s => String(s || "").replace(/\D/g, "");
  const nrm = s => String(s || "").normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();
  const av = (c, cls = "") => c.photo
    ? `<div class="av ${cls}"><img loading="lazy" src="${base}/contact/${c.id}/photo?thumb=1" alt=""></div>`
    : `<div class="av ${cls}" style="background:${color(c.name)}">${initials(c.name)}</div>`;
  api(base + "/contacts").then(r => { all = r; draw(); }).catch(e => list.innerHTML = `<div class="empty">${esc(e.message)}</div>`);
  input.oninput = draw;
  function draw() {
    const q = nrm(input.value.trim()), qd = digits(q);
    const items = all.filter(c => !q || nrm(c.name + " " + (c.org || "") + " " + c.emails.map(e => e.value).join(" ")).includes(q)
      || (qd.length >= 3 && c.phones.some(p => digits(p.value).includes(qd))));
    $(".cnt", el).textContent = `${num(items.length)} contatos`;
    let letter = null;
    list.innerHTML = items.map(c => {
      const L = nrm(c.name)[0]?.toUpperCase() || "#";
      const head = L !== letter ? `<div class="sys" style="padding:6px 12px;text-align:left;font-weight:600">${esc((letter = L))}</div>` : "";
      return head + `<div class="item${cur === c.id ? " on" : ""}" data-id="${c.id}">${av(c)}
        <div class="ci" style="flex:1;min-width:0"><div class="n" style="font-weight:600">${esc(c.name)}</div>
        <div class="l">${esc(c.org && c.org !== c.name ? c.org : (c.phones[0] || c.emails[0] || {}).value || "")}</div></div></div>`;
    }).join("") || `<div class="empty">Nenhum contato</div>`;
  }
  list.onclick = e => { const it = e.target.closest(".item"); if (it) open(all.find(c => c.id === +it.dataset.id)); };
  function open(c) {
    cur = c.id; $$(".item", list).forEach(i => i.classList.toggle("on", +i.dataset.id === c.id));
    el.classList.add("open");
    const rows = (title, arr, fmt) => arr.length ? `<div class="sec">${arr.map(x => `<div class="row2"><div class="k">${esc(x.label || title)}</div><div class="v">${fmt(x.value)}</div></div>`).join("")}</div>` : "";
    const tel = v => `<a href="tel:${esc(v.replace(/[^\d+]/g, ""))}">${esc(v)}</a>`;
    const mail = v => `<a href="mailto:${esc(v)}">${esc(v)}</a>`;
    const link = v => /^https?:\/\//i.test(v) ? `<a href="${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a>` : esc(v);
    const info = [
      c.nick && {label: "Apelido", value: c.nick}, c.job && {label: "Cargo", value: c.job}, c.dept && {label: "Departamento", value: c.dept},
      c.birthday && {label: "Aniversário", value: c.birthday}, ...c.dates.map(d => ({label: d.label || "Data", value: d.value})),
      ...c.related.map(r => ({label: r.label || "Relacionado", value: r.value})), ...c.ims, ...c.social,
    ].filter(Boolean);
    det.innerHTML = `<div class="inner cdetail">
      <div class="dhead"><button class="btn back2" style="display:none">‹</button>
        ${c.photo ? `<div class="av big"><img src="${base}/contact/${c.id}/photo" data-full="${base}/contact/${c.id}/photo?x=1" style="cursor:pointer"></div>` : av(c, "big")}
        <div style="flex:1;min-width:0"><h2>${esc(c.name)}</h2>${c.org && c.org !== c.name ? `<div style="color:var(--muted)">${esc(c.org)}</div>` : ""}</div>
        <a class="btn" href="${base}/contacts.vcf?id=${c.id}">⬇ Exportar .vcf</a></div>
      ${rows("Telefone", c.phones, tel)}${rows("E-mail", c.emails, mail)}${rows("Endereço", c.addresses, esc)}${rows("Site", c.urls, link)}
      ${rows("", info, esc)}
      ${c.note ? `<div class="sec"><div class="row2"><div class="k">Nota</div><div class="v">${esc(c.note)}</div></div></div>` : ""}
      <div class="meta-line">Criado em ${fmtDT(c.created)} · Modificado em ${fmtDT(c.modified)}</div></div>`;
    $(".back2", det).onclick = () => el.classList.remove("open");
  }
  imgLightbox(det);
  return {el};
}

/* ---------- Explorador de apps ---------- */
const TYPE_ICON = {sqlite: "🗄", plist: "⚙️", image: "🖼", heic: "🖼", video: "🎥", audio: "🎵", pdf: "📕", text: "📄", json: "📄", binary: "📦"};
function guessIcon(name) {
  const e = (name.split(".").pop() || "").toLowerCase();
  return {sqlite: "🗄", sqlitedb: "🗄", db: "🗄", storedata: "🗄", sqlite3: "🗄", plist: "⚙️", jpg: "🖼", jpeg: "🖼", png: "🖼", heic: "🖼", gif: "🖼", webp: "🖼",
          mov: "🎥", mp4: "🎥", m4a: "🎵", mp3: "🎵", opus: "🎵", caf: "🎵", pdf: "📕", json: "📄", txt: "📄", log: "📄", xml: "📄", html: "📄"}[e] || "📄";
}
function fmtCell(v, col) {
  if (v === null || v === undefined) return `<span class="null">NULL</span>`;
  if (typeof v === "object") {
    if ("blob" in v) return `<span class="blob" data-col="${esc(col)}">◆ ${v.kind} · ${fmtSize(v.blob)}</span>`;
    if ("text" in v) return esc(v.text) + `… <span class="null">(${num(v.len)} car.)</span>`;
  }
  if (typeof v === "number" && /date|time|stamp/i.test(col)) {
    let t = null;
    if (v > 3e8 && v < 1.2e9) t = v + 978307200;           // segundos desde 2001 (Apple)
    else if (v >= 1.2e9 && v < 2.2e9) t = v;                // Unix
    else if (v >= 1.2e12 && v < 2.2e12) t = v / 1000;       // Unix em ms
    else if (v >= 3e17 && v < 1.2e18) t = v / 1e9 + 978307200; // nanossegundos desde 2001
    if (t) return `<span title="${esc(fmtDT(t))}">${v}</span> <span class="null">${esc(fmtDT(t))}</span>`;
  }
  return esc(v);
}
function makeApps() {
  const el = makeView("apps-view", `
    <div class="col applist" style="width:290px">
      <div class="searchrow"><input type="search" placeholder="Buscar app…"></div>
      <div class="listbar"><span class="cnt"></span></div>
      <div class="list"><div class="empty">Lendo o conteúdo do backup…</div></div>
    </div>
    <div class="col files" style="width:380px"><div class="empty">Selecione um app</div></div>
    <div class="detail"><div class="empty">Selecione um arquivo para visualizar</div></div>`);
  const S = {entries: [], entry: null, domain: null, dir: "", file: null};
  const list = $(".applist .list", el), filesEl = $(".files", el), det = $(".detail", el), input = $(".applist input", el);
  api(base + "/explorer").then(r => { S.entries = r; drawApps(); }).catch(e => list.innerHTML = `<div class="empty">${esc(e.message)}</div>`);
  input.oninput = drawApps;
  function drawApps() {
    const q = input.value.trim().toLowerCase();
    const items = S.entries.filter(e => !q || e.name.toLowerCase().includes(q) || (e.bid || "").toLowerCase().includes(q));
    $(".applist .cnt", el).textContent = `${num(items.length)} apps e áreas do sistema`;
    list.innerHTML = items.map(e => `
      <div class="item${S.entry === e ? " on" : ""}" data-k="${esc(e.key)}">
        <div class="ic">${e.icon ? `<img loading="lazy" src="${base}/appicon/${encodeURIComponent(e.bid)}">` : e.kind === "sistema" ? "⚙️" : "📱"}</div>
        <div class="ci" style="flex:1;min-width:0"><div class="n" style="font-weight:600">${esc(e.name)}</div>
          <div class="l" style="font-size:12px">${e.kind === "sistema" ? "Sistema" : esc(e.bid || "")}</div></div>
        <span class="sz">${fmtSize(e.bytes)}</span></div>`).join("");
  }
  list.onclick = ev => {
    const it = ev.target.closest(".item"); if (!it) return;
    S.entry = S.entries.find(e => e.key === it.dataset.k);
    $$(".item", list).forEach(i => i.classList.toggle("on", i === it));
    el.classList.add("open2"); el.classList.remove("open");
    if (S.entry.domains.length === 1) browse(S.entry.domains[0].domain, ""); else browse(null, "");
  };
  async function browse(domain, dir) {
    S.domain = domain; S.dir = dir;
    const e = S.entry;
    const crumbs = [`<button class="btn back2" style="display:none" data-back="1">‹</button><a data-d="" data-dom="">${esc(e.name)}</a>`];
    if (domain && e.domains.length > 1) crumbs.push(`<a data-dom="${esc(domain)}" data-d="">${esc(domLabel(domain))}</a>`);
    let acc = "";
    for (const p of dir.split("/").filter(Boolean)) { acc += (acc ? "/" : "") + p; crumbs.push(`<a data-dom="${esc(domain)}" data-d="${esc(acc)}">${esc(p)}</a>`); }
    let body;
    if (!domain) {
      body = e.domains.map(d => `<div class="item" data-dom="${esc(d.domain)}"><div class="ic">📂</div>
        <div class="ci" style="flex:1;min-width:0"><div class="n">${esc(domLabel(d.domain))}</div><div class="l" style="font-size:11.5px">${esc(d.domain)}</div></div>
        <span class="sz">${fmtSize(d.bytes)}</span></div>`).join("");
    } else {
      filesEl.innerHTML = `<div class="crumbs">${crumbs.join(" / ")}</div><div class="list"><div class="empty">Carregando…</div></div>`;
      const r = await api(`${base}/explorer/ls?domain=${encodeURIComponent(domain)}&dir=${encodeURIComponent(dir)}`);
      body = r.dirs.map(d => `<div class="item" data-sub="${esc(d.name)}"><div class="ic">📁</div>
          <div class="ci" style="flex:1;min-width:0"><div class="n">${esc(d.name)}</div><div class="l" style="font-size:11.5px">${num(d.files)} arquivos</div></div>
          <span class="sz">${fmtSize(d.bytes)}</span></div>`).join("") +
        r.files.map(f => `<div class="item${S.file === (dir ? dir + "/" : "") + f.name ? " on" : ""}" data-file="${esc(f.name)}"><div class="ic">${guessIcon(f.name)}</div>
          <div class="ci" style="flex:1;min-width:0"><div class="n">${esc(f.name)}</div></div><span class="sz">${fmtSize(f.size)}</span></div>`).join("")
        || `<div class="empty">Pasta vazia</div>`;
    }
    filesEl.innerHTML = `<div class="crumbs">${crumbs.join(" / ")}</div><div class="list">${body}</div>`;
  }
  const domLabel = d => ({app: "Dados do app", grupo: "Dados compartilhados (grupo)", "extensão": "Extensão"}[(S.entry.domains.find(x => x.domain === d) || {}).kind] || "Dados") ;
  filesEl.onclick = ev => {
    if (ev.target.dataset.back) { el.classList.remove("open2"); return; }
    const a = ev.target.closest("a[data-d]");
    if (a) return browse(a.dataset.dom || null, a.dataset.d);
    const it = ev.target.closest(".item"); if (!it) return;
    if (it.dataset.dom) return browse(it.dataset.dom, "");
    if (it.dataset.sub) return browse(S.domain, (S.dir ? S.dir + "/" : "") + it.dataset.sub);
    if (it.dataset.file) {
      $$(".item", filesEl).forEach(i => i.classList.toggle("on", i === it));
      openFile((S.dir ? S.dir + "/" : "") + it.dataset.file);
    }
  };
  async function openFile(path) {
    S.file = path;
    el.classList.add("open"); el.classList.remove("open2");
    const dom = S.domain, q = `domain=${encodeURIComponent(dom)}&path=${encodeURIComponent(path)}`;
    const raw = `${base}/raw?${q}`;
    det.innerHTML = `<div class="empty">Carregando…</div>`;
    let f;
    try { f = await api(`${base}/explorer/info?${q}`); } catch (e) { det.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
    let body = "";
    const t = f.type;
    if (t === "image") body = `<img src="${raw}" data-full="${raw}&x=1" style="cursor:pointer">`;
    else if (t === "heic") body = B.heic ? `<img src="${raw}&jpg=1">` : `<p>Imagem HEIC — instale pillow-heif para visualizar.</p>`;
    else if (t === "video") body = `<video controls src="${raw}"></video>`;
    else if (t === "audio") body = `<audio controls src="${raw}"></audio>`;
    else if (t === "pdf") body = `<iframe src="${raw}"></iframe>`;
    else if (t === "plist" || t === "gzip:plist") body = `<pre>${esc(JSON.stringify(f.json, null, 2))}</pre>`;
    else if (t === "text" || t === "json" || t === "gzip:text") {
      let txt = f.text;
      if (t === "json") { try { txt = JSON.stringify(JSON.parse(txt), null, 2); } catch (e) {} }
      body = `<pre>${esc(txt)}</pre>${f.truncated ? `<p class="null">Mostrando só o início do arquivo.</p>` : ""}`;
    }
    else if (t === "sqlite") body = `<div class="sqlv"></div>`;
    else body = `<p style="color:var(--muted)">${f.note ? esc(f.note) + " · " : ""}Arquivo binário — prévia hexadecimal dos primeiros bytes:</p><pre>${esc(f.hex || "")}</pre>`;
    det.innerHTML = `<div class="prev">
      <div class="dhead"><button class="btn back2" style="display:none">‹</button><h2 style="font-size:16px">${TYPE_ICON[t.split(":").pop()] || "📄"} ${esc(f.name)}</h2>
        <span class="badge2">${esc(t)}</span><span class="badge2">${fmtSize(f.size)}</span><a class="btn" href="${raw}&dl=1">⬇ Baixar</a></div>
      <div class="meta-line">${esc(dom)} / ${esc(path)}</div>${body}</div>`;
    $(".back2", det).onclick = () => { el.classList.remove("open"); el.classList.add("open2"); };
    imgLightboxOnce(det);
    if (t === "sqlite") sqliteView($(".sqlv", det), dom, path, f.tables);
  }
  function imgLightboxOnce(c) { if (!c.dataset.lb) { c.dataset.lb = 1; imgLightbox(c); } }
  function sqliteView(box, dom, path, tables) {
    const q = `domain=${encodeURIComponent(dom)}&path=${encodeURIComponent(path)}`;
    const T = {table: (tables.find(t => t.rows) || tables[0] || {}).name, offset: 0};
    box.innerHTML = `
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
        <select class="tsel">${tables.map(t => `<option value="${esc(t.name)}">${esc(t.name)} (${t.rows == null ? "?" : num(t.rows)})</option>`).join("")}</select>
        <button class="btn" data-p="-1">‹ Anterior</button><button class="btn" data-p="1">Próxima ›</button><span class="pinfo null"></span>
        <button class="btn" data-sql="1" style="margin-left:auto">Consulta SQL…</button></div>
      <div class="sqlbox" hidden><textarea placeholder="SELECT * FROM tabela WHERE … (somente leitura)"></textarea><button class="btn primary" data-run="1">Executar</button></div>
      <div class="grid-wrap"></div>`;
    const sel = $(".tsel", box), wrap = $(".grid-wrap", box);
    if (T.table) sel.value = T.table;
    async function load(sql) {
      wrap.innerHTML = `<div class="empty">Carregando…</div>`;
      let r;
      try {
        r = await api(`${base}/explorer/rows?${q}` + (sql ? `&sql=${encodeURIComponent(sql)}` : `&table=${encodeURIComponent(T.table)}&offset=${T.offset}&limit=100`));
      } catch (e) { wrap.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
      T.sql = !!sql;
      const total = (tables.find(t => t.name === T.table) || {}).rows;
      $(".pinfo", box).textContent = sql ? `${num(r.rows.length)}${r.more ? "+" : ""} linhas` : total == null ? "" : `linhas ${num(Math.min(T.offset + 1, total))}–${num(Math.min(T.offset + 100, total))} de ${num(total)}`;
      const rid = r.cols.indexOf("__rowid__");
      wrap.innerHTML = r.rows.length ? `<table class="grid"><tr>${r.cols.map((c, i) => i === rid ? "" : `<th>${esc(c)}</th>`).join("")}</tr>` +
        r.rows.map(row => `<tr data-rowid="${rid >= 0 ? row[rid] : ""}">${row.map((v, i) => i === rid ? "" : `<td class="${typeof v === "number" ? "num" : ""}">${fmtCell(v, r.cols[i])}</td>`).join("")}</tr>`).join("") + `</table>`
        : `<div class="empty">Sem linhas</div>`;
    }
    sel.onchange = () => { T.table = sel.value; T.offset = 0; load(); };
    box.addEventListener("click", async ev => {
      const p = ev.target.dataset.p;
      if (p) { T.offset = Math.max(0, T.offset + 100 * +p); load(); }
      if (ev.target.dataset.sql) { const sb = $(".sqlbox", box); sb.hidden = !sb.hidden; if (!sb.hidden) { $("textarea", sb).value ||= `SELECT * FROM "${T.table}" LIMIT 100`; $("textarea", sb).focus(); } }
      if (ev.target.dataset.run) load($(".sqlbox textarea", box).value);
      const bl = ev.target.closest(".blob");
      if (bl) {
        const rowid = bl.closest("tr").dataset.rowid;
        if (!rowid || T.sql) return toast("Abra a tabela (não a consulta SQL) para ver o conteúdo do blob");
        const bq = `${base}/explorer/blob?${q}&table=${encodeURIComponent(T.table)}&rowid=${rowid}&col=${encodeURIComponent(bl.dataset.col)}`;
        const d = await api(bq);
        const html = d.kind === "image" ? `<img src="${bq}&raw=1">`
          : `<pre style="max-width:90vw;max-height:80vh;overflow:auto;background:#111;color:#eee;padding:12px;border-radius:8px;white-space:pre-wrap">${esc(d.kind === "plist" ? JSON.stringify(d.json, null, 2) : d.text || d.hex)}</pre>`;
        openLb([1], 0, () => ({html, caption: `${T.table}.${bl.dataset.col} · ${d.kind}`, actions: []}));
      }
    });
    if (T.table) load(); else wrap.innerHTML = `<div class="empty">Banco sem tabelas</div>`;
  }
  return {el};
}

/* ---------- Encerrar / manter vivo ---------- */
// O servidor fecha sozinho se esta janela parar de avisar que está aberta.
const showDead = () => { $("#dead").classList.add("on"); clearInterval(pingT); };
const pingT = setInterval(() => fetch("/api/ping").then(r => { if (!r.ok) throw 0; }).catch(showDead), 15000);
$("#quit").onclick = async () => {
  if (!await ask({title: "Sair do Garimpo?", text: "Suas pastas e exclusões já estão salvas.", ok: "Sair"})) return;
  try { await post("/api/quit", {}); } catch (e) { return toast(e.message); }
  showDead(); setTimeout(() => window.close(), 300);
};

init().catch(e => $("#device").textContent = "Erro: " + e.message);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
