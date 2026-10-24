#!/usr/bin/env python3
"""Tira os prints do README a partir do backup FICTÍCIO, controlando o Edge/Chrome sem janela (protocolo DevTools).

Uso:  python tools/capturar_telas.py [PASTA_DE_SAIDA]        (padrão: docs/img)
Requer: Microsoft Edge ou Google Chrome e  python -m pip install pillow
Também falha se o navegador acusar violação da política de segurança (CSP) ou erro de JavaScript.
"""
import base64
import json
import os
import pathlib
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tools"))
import garimpo  # noqa: E402
import gerar_backup_demo  # noqa: E402
from PIL import Image  # noqa: E402


class WebSocket:
    """Cliente WebSocket mínimo (só o necessário para falar com o DevTools do navegador)."""

    def __init__(self, url):
        host, _, resto = url[len("ws://"):].partition("/")
        h, _, p = host.partition(":")
        self.s = socket.create_connection((h, int(p)), timeout=60)
        chave = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET /{resto} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {chave}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.s.recv(4096)
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            raise RuntimeError("O navegador recusou a conexão do DevTools")
        self.buf = buf.split(b"\r\n\r\n", 1)[1]
        self.prox_id = 0
        self.eventos = []

    def _ler(self, n):
        while len(self.buf) < n:
            parte = self.s.recv(1 << 20)
            if not parte:
                raise EOFError("conexão encerrada")
            self.buf += parte
        saida, self.buf = self.buf[:n], self.buf[n:]
        return saida

    def _enviar(self, texto):
        dados = texto.encode()
        cab = bytearray([0x81])
        n = len(dados)
        cab += bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536 else bytes([0x80 | 127]) + struct.pack(">Q", n)
        mascara = os.urandom(4)
        self.s.sendall(bytes(cab) + mascara + bytes(b ^ mascara[i % 4] for i, b in enumerate(dados)))

    def _receber(self):
        corpo = b""
        while True:
            b1, b2 = self._ler(2)
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._ler(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._ler(8))[0]
            dados = self._ler(n)
            if b1 & 0x0F == 8:
                raise EOFError("conexão encerrada")
            if b1 & 0x0F in (1, 2, 0):
                corpo += dados
                if b1 & 0x80:
                    return json.loads(corpo)

    def cmd(self, metodo, **params):
        self.prox_id += 1
        self._enviar(json.dumps({"id": self.prox_id, "method": metodo, "params": params}))
        while True:
            msg = self._receber()
            if msg.get("id") == self.prox_id:
                if "error" in msg:
                    raise RuntimeError(f"{metodo}: {msg['error']}")
                return msg.get("result", {})
            self.eventos.append(msg)


def porta_livre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def esperar(fn, tempo=60, intervalo=0.3):
    fim = time.time() + tempo
    while time.time() < fim:
        try:
            r = fn()
            if r:
                return r
        except Exception:
            pass
        time.sleep(intervalo)
    raise TimeoutError("tempo esgotado")


def main():
    saida = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else RAIZ / "docs" / "img"
    saida.mkdir(parents=True, exist_ok=True)
    navegador = garimpo.find_app_browser()
    if not navegador:
        sys.exit("Instale o Microsoft Edge ou o Google Chrome para tirar os prints.")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="garimpo_prints_"))
    servidor = nav = None
    try:
        raiz_bk, _ = gerar_backup_demo.gerar(tmp / "backups")
        p_app = porta_livre()
        servidor = subprocess.Popen([sys.executable, str(RAIZ / "garimpo.py"), "--no-browser", "--port", str(p_app),
                                     "--backup-dir", str(tmp / "backups"), "--dados", str(tmp / "dados")],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{p_app}"
        esperar(lambda: json.load(urllib.request.urlopen(f"{base}/api/ping", timeout=2)).get("app") == "garimpo")

        def api(caminho, corpo=None):
            req = urllib.request.Request(base + caminho, data=json.dumps(corpo).encode() if corpo is not None else None,
                                         headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req, timeout=120))

        bid = raiz_bk.name
        # organiza as conversas como no uso real: importantes, pasta própria, e as de 1 mensagem ficam em "Inúteis"
        chats = {c["name"]: c for c in api(f"/api/b/{bid}/wa/wa/chats")["chats"]}
        api("/api/wa/wa/action", {"op": "move", "chats": [chats["Ana Souza"]["jid"], chats["Carlos Mendes"]["jid"]], "folder": "importantes"})
        nova = api("/api/wa/wa/action", {"op": "folder_create", "name": "Trabalho"})["id"]
        api("/api/wa/wa/action", {"op": "move", "chats": [chats["Time do Projeto"]["jid"]], "folder": nova})

        dbg = porta_livre()
        nav = subprocess.Popen([navegador, "--headless=new", f"--remote-debugging-port={dbg}", f"--user-data-dir={tmp / 'perfil'}",
                                "--no-first-run", "--no-default-browser-check", "--disable-gpu", "--hide-scrollbars",
                                "--disable-extensions", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        alvo = esperar(lambda: next(t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{dbg}/json/list", timeout=2))
                                    if t["type"] == "page"))
        ws = WebSocket(alvo["webSocketDebuggerUrl"])
        for dom in ("Page", "Runtime", "Log", "Network"):
            ws.cmd(f"{dom}.enable")
        ws.cmd("Emulation.setDeviceMetricsOverride", width=1400, height=880, deviceScaleFactor=1.5, mobile=False)

        def js(codigo):
            r = ws.cmd("Runtime.evaluate", expression=f"(async () => {{ const W = ms => new Promise(r => setTimeout(r, ms)); "
                       f"const esperar = async (f, t = 15000) => {{ const fim = Date.now() + t; while (Date.now() < fim) {{ const v = f(); if (v) return v; await W(150); }} throw new Error('timeout: ' + f); }}; "
                       f"{codigo} }})()", awaitPromise=True, returnByValue=True)
            if "exceptionDetails" in r:
                raise RuntimeError(r["exceptionDetails"].get("exception", {}).get("description") or str(r["exceptionDetails"]))
            return r["result"].get("value")

        def abrir(escuro=False):
            ws.cmd("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark" if escuro else "light"}])
            ws.cmd("Page.navigate", url=base + "/")
            js("await esperar(() => document.querySelector('#tabs button'))")

        def foto(nome):
            time.sleep(0.8)
            png = base64.b64decode(ws.cmd("Page.captureScreenshot", format="png", fromSurface=True)["data"])
            destino = saida / f"{nome}.png"
            destino.write_bytes(png)
            Image.open(destino).save(destino, optimize=True)
            print(f"  {destino.name}  ({destino.stat().st_size // 1024} KB)")

        aba = lambda k: js(f"document.querySelector('#tabs [data-k=\"{k}\"]').click()")
        clicar_texto = lambda sel, txt: js(f"const el = await esperar(() => [...document.querySelectorAll({sel!r})].find(e => e.textContent.trim().startsWith({txt!r}))); el.click();")

        print("Tirando os prints…")
        abrir()
        js("await esperar(() => document.querySelectorAll('.tile img').length > 8); await W(1500)")
        foto("fotos")

        aba("wa")
        js("await esperar(() => document.querySelector('.view.wa.on .fitem[data-f=\"grupos\"]'))")
        clicar_texto(".view.wa.on .chat .n", "Grupo da Família")
        js("await esperar(() => document.querySelectorAll('.view.wa.on .msg').length > 5); await W(1500)")
        foto("whatsapp-conversa")

        js("document.querySelector('.view.wa.on .fitem[data-f=\"inuteis\"]').click(); await W(500)")
        foto("whatsapp-pastas")

        js("document.querySelector('.view.wa.on .fitem[data-f=\"media\"]').click(); await esperar(() => document.querySelectorAll('.view.wa.on .mpane .tile').length > 5); await W(1500)")
        foto("midias-por-tamanho")

        js("""const V = document.querySelector('.view.wa.on');
              V.querySelector('.fitem[data-f="todas"]').click(); await W(300);
              V.querySelector('.seg [data-m="msgs"]').click();
              const i = V.querySelector('.searchrow input'); i.value = 'reuniao';
              i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'}));
              await esperar(() => V.querySelectorAll('.result').length > 2)""")
        foto("busca-no-texto")

        aba("notes")
        js("await esperar(() => document.querySelectorAll('.notes-view .list .item').length > 2)")
        clicar_texto(".notes-view .list .item .n", "Ideias para o projeto")
        js("await esperar(() => document.querySelector('.notes-view .notebody img')); await W(1200)")
        foto("notas")

        aba("contacts")
        js("await esperar(() => document.querySelectorAll('.contacts-view .list .item').length > 5)")
        clicar_texto(".contacts-view .list .item .n", "Ana Souza")
        js("await esperar(() => document.querySelector('.contacts-view .detail .big img')); await W(600)")
        foto("contatos")

        aba("apps")
        js("await esperar(() => document.querySelectorAll('.apps-view .applist .item').length > 3)")
        clicar_texto(".apps-view .applist .item .n", "Receitas da Vovó")
        clicar_texto(".apps-view .files .item .n", "Documents")
        clicar_texto(".apps-view .files .item .n", "receitas.sqlite")
        js("await esperar(() => document.querySelectorAll('.apps-view table.grid tr').length > 3); await W(800)")
        foto("explorador-de-apps")

        abrir(escuro=True)
        aba("wa")
        js("await esperar(() => document.querySelector('.view.wa.on .fitem[data-f=\"grupos\"]'))")
        clicar_texto(".view.wa.on .chat .n", "Time do Projeto")
        js("await esperar(() => document.querySelectorAll('.view.wa.on .msg').length > 5); await W(1200)")
        foto("whatsapp-escuro")

        problemas = []
        for e in ws.eventos:
            if e.get("method") == "Runtime.exceptionThrown":
                problemas.append("exceção JS: " + str(e["params"]["exceptionDetails"].get("exception", {}).get("description", ""))[:200])
            elif e.get("method") == "Log.entryAdded" and e["params"]["entry"]["level"] in ("error", "warning"):
                problemas.append(f"{e['params']['entry']['level']}: {e['params']['entry']['text'][:200]}")
            elif e.get("method") == "Network.responseReceived" and e["params"]["response"]["status"] >= 400:
                r = e["params"]["response"]
                problemas.append(f"HTTP {r['status']}: {r['url'].replace(base, '')[:160]}")
            elif e.get("method") == "Runtime.consoleAPICalled" and e["params"]["type"] == "error":
                problemas.append("console.error: " + " ".join(str(a.get("value", a.get("description", ""))) for a in e["params"]["args"])[:200])
        if problemas:
            print("\nProblemas no navegador:")
            for p in dict.fromkeys(problemas):
                print("  -", p)
            sys.exit(1)
        print("Sem erros de JavaScript nem violações de CSP no navegador.")
    finally:
        for p in (nav, servidor):
            if p:
                p.terminate()
        time.sleep(1)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
