#!/usr/bin/env python3
"""Gera um backup de iPhone FICTÍCIO (tudo inventado) no mesmo formato do iTunes / Dispositivos Apple.

Serve para demonstrar o Garimpo, tirar prints sem expor dados reais e rodar os testes.
Nomes, telefones (DDD 00, que não existe), mensagens, fotos e notas são sintéticos.

Uso:
    python tools/gerar_backup_demo.py demo            # cria demo/DEMO0000-0000000000000000/
    python garimpo.py --backup-dir demo --dados demo/dados

Requer: python -m pip install pillow
"""
import argparse
import datetime as dt
import gzip
import hashlib
import io
import math
import os
import pathlib
import plistlib
import random
import shutil
import sqlite3
import tempfile

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    raise SystemExit("Este gerador precisa do Pillow. Instale com:  python -m pip install pillow")

UDID = "DEMO0000-0000000000000000"
EPOCH = dt.datetime(2001, 1, 1)
FIM = dt.datetime(2026, 10, 19, 18, 30)  # "agora" do backup fictício


def apple(d):
    return (d - EPOCH).total_seconds()


# ---------------------------------------------------------------------------- arquivos do backup
class Backup:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.rows = []

    def add(self, domain, rel, data):
        fid = hashlib.sha1(f"{domain}-{rel}".encode()).hexdigest()
        d = self.root / fid[:2]
        d.mkdir(exist_ok=True)
        (d / fid).write_bytes(data)
        self.rows.append((fid, domain, rel, 1, b""))

    def finish(self, info):
        db = sqlite3.connect(self.root / "Manifest.db")
        db.executescript("create table Files (fileID text primary key, domain text, relativePath text, flags integer, file blob);"
                         "create table Properties (key text primary key, value blob);")
        db.executemany("insert into Files values (?,?,?,?,?)", self.rows)
        db.commit()
        db.close()
        (self.root / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False, "Version": "10.0"}))
        (self.root / "Info.plist").write_bytes(plistlib.dumps(info))


def sqlite_bytes(build):
    with tempfile.TemporaryDirectory() as t:
        p = pathlib.Path(t) / "x.sqlite"
        db = sqlite3.connect(p)
        build(db)
        db.commit()
        db.close()
        return p.read_bytes()


# ---------------------------------------------------------------------------- imagens sintéticas
def fonte(tam):
    try:
        return ImageFont.load_default(size=tam)
    except TypeError:
        return ImageFont.load_default()


def jpeg(im, q=85):
    b = io.BytesIO()
    im.convert("RGB").save(b, "JPEG", quality=q)
    return b.getvalue()


def png(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def paisagem(rng, w=800, h=600, ruido=0.0):
    topo = tuple(rng.randint(20, 120) for _ in range(3))
    base = tuple(rng.randint(150, 255) for _ in range(3))
    im = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=tuple(int(topo[i] * (1 - t) + base[i] * t) for i in range(3)))
    sx, sy, r = rng.randint(w // 8, w * 7 // 8), rng.randint(h // 10, h // 2), rng.randint(h // 14, h // 8)
    d.ellipse([sx - r, sy - r, sx + r, sy + r], fill=(255, 241, 205))
    for camada in range(3):
        y0 = h * (0.55 + 0.12 * camada)
        amp, fr, ph = h * (0.07 + 0.02 * camada), rng.uniform(0.004, 0.012) * 800 / w, rng.random() * 6
        cor = tuple(max(0, min(255, int((c + 40) * (0.62 - 0.14 * camada)))) for c in (topo[0] + 40, topo[1] + 70, topo[2] + 20))
        pts = [(0, h)] + [(x, y0 + amp * math.sin(x * fr + ph)) for x in range(0, w + 1, 6)] + [(w, h)]
        d.polygon(pts, fill=cor)
    if ruido:  # deixa o arquivo mais pesado, para demonstrar a ordenação por tamanho
        im = Image.blend(im, Image.frombytes("RGB", (w, h), rng.randbytes(w * h * 3)), ruido)
    return im


def miniatura(im, lado=256):
    w, h = im.size
    m = min(w, h)
    return im.crop(((w - m) // 2, (h - m) // 2, (w + m) // 2, (h + m) // 2)).resize((lado, lado))


def avatar(nome, cor, lado=240):
    im = Image.new("RGB", (lado, lado), cor)
    d = ImageDraw.Draw(im)
    d.text((lado / 2, lado / 2), nome.strip()[0].upper(), fill="white", font=fonte(lado // 2), anchor="mm")
    return im


def icone_app(letra, cor, lado=120):
    im = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, lado - 1, lado - 1], radius=lado // 4, fill=cor)
    d.text((lado / 2, lado / 2), letra, fill="white", font=fonte(lado // 2), anchor="mm")
    return im


def pdf_falso(rng, tamanho):
    return b"%PDF-1.4\n% arquivo ficticio de demonstracao\n" + rng.randbytes(max(0, tamanho - 60)) + b"\n%%EOF\n"


# ---------------------------------------------------------------------------- protobuf das Notas
def varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def f_int(campo, v):
    return varint(campo << 3) + varint(v)


def f_bytes(campo, b):
    return varint(campo << 3 | 2) + varint(len(b)) + b


class Nota:
    """Monta o conteúdo de uma nota no formato interno do app Notas (protobuf + gzip)."""

    def __init__(self):
        self.texto, self.runs = "", []

    def add(self, texto, estilo=-1, peso=0, feito=None, att=None, indent=0):
        n = len(texto.encode("utf-16-le")) // 2
        para = b""
        if estilo >= 0:
            para += f_int(1, estilo)
        if indent:
            para += f_int(4, indent)
        if feito is not None:
            para += f_bytes(5, f_bytes(1, os.urandom(16)) + f_int(2, 1 if feito else 0))
        run = f_int(1, n) + (f_bytes(2, para) if para else b"")
        if peso:
            run += f_int(5, peso)
        if att:
            run += f_bytes(12, f_bytes(1, att[0].encode()) + f_bytes(2, att[1].encode()))
        self.texto += texto
        self.runs.append(run)
        return self

    def linha(self, texto, **kw):
        return self.add(texto + "\n", **kw)

    def dados(self):
        corpo = f_bytes(2, self.texto.encode()) + b"".join(f_bytes(5, r) for r in self.runs)
        doc = f_int(1, 0) + f_bytes(2, f_int(1, 0) + f_int(2, 0) + f_bytes(3, corpo))
        return gzip.compress(doc)


# ---------------------------------------------------------------------------- Fotos
def gerar_fotos(bk, rng):
    linhas = []
    hoje = FIM - dt.timedelta(hours=3)
    total = 40
    for i in range(total):
        video = i in (7, 19, 33)
        nome = f"IMG_{i + 1:04d}.{'MOV' if video else 'JPG'}"
        data = hoje - dt.timedelta(days=i * 3.1 + rng.random() * 2, hours=rng.randint(0, 8))
        im = paisagem(rng, 1200, 900) if i % 2 == 0 else paisagem(rng, 900, 1200)
        sem_original = video or i in (11, 26)  # como se estivessem só no iCloud
        diretorio = "DCIM/100APPLE"
        if not sem_original:
            bk.add("CameraRollDomain", f"Media/{diretorio}/{nome}", jpeg(im, 88))
        bk.add("CameraRollDomain", f"Media/PhotoData/Thumbnails/V2/{diretorio}/{nome}/5005.JPG", jpeg(miniatura(im), 80))
        linhas.append((i + 1, diretorio, nome, 1 if video else 0, apple(data), 1 if i in (2, 9, 14, 21, 30) else 0,
                       1 if i == 38 else 0, im.size[0], im.size[1], 12.0 + i if video else None, 0, 0, 0))

    def build(db):
        db.execute("create table ZASSET (Z_PK integer primary key, ZDIRECTORY varchar, ZFILENAME varchar, ZKIND integer, "
                   "ZDATECREATED timestamp, ZFAVORITE integer, ZHIDDEN integer, ZWIDTH integer, ZHEIGHT integer, "
                   "ZDURATION float, ZTRASHEDSTATE integer, ZCLOUDLOCALSTATE integer, ZSAVEDASSETTYPE integer)")
        db.executemany("insert into ZASSET values (?,?,?,?,?,?,?,?,?,?,?,?,?)", linhas)
    bk.add("CameraRollDomain", "Media/PhotoData/Photos.sqlite", sqlite_bytes(build))
    return total


# ---------------------------------------------------------------------------- WhatsApp
WA = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"
NUM = lambda n: f"55009123400{n:02d}"

FILLER = [
    "Oi, tudo bem?", "Tudo certo por aqui!", "Vi sua mensagem agora", "Já te respondo", "Beleza 👍", "Combinado",
    "Pode ser", "Que ótimo!", "Bom dia!", "Boa noite, pessoal", "Depois a gente conversa", "Estou chegando",
    "Obrigado!", "De nada 😊", "Hahaha", "Verdade", "Não sabia disso", "Me manda por favor", "Já enviei",
    "Vamos marcar?", "Semana que vem eu consigo", "Estou em reunião, falo depois", "Acabei de sair",
    "Qual o horário mesmo?", "Perfeito", "Fechado!", "Pode deixar", "Valeu pela ajuda", "Depois eu confiro",
]

ROTEIROS = {
    "Ana Souza": [("o", "Oi! Chegou bem em casa?"), ("e", "Cheguei sim, obrigado por ontem 😊"),
                  ("o", "Imagina! Foi ótimo. Olha a foto que tirei", "foto"), ("e", "Ficou linda!"),
                  ("o", "Vamos repetir sábado?"), ("e", "Bora! Que horas?"), ("o", "Por volta das 15h, no mesmo café"),
                  ("e", "Fechado ✅")],
    "Grupo da Família": [("Mãe", "Bom dia, família! ☀️"), ("Pai", "Bom dia! Alguém vai ao almoço de domingo?"),
                         ("e", "Eu vou! Levo a sobremesa"), ("Tio Roberto", "Eu também. Vou levar o violão 🎸"),
                         ("Beatriz", "Olhem a foto que tirei na praia ontem", "foto"), ("Mãe", "Que lindo!! ❤️"),
                         ("Pai", "Obrigado por avisar, vou confirmar a reserva"), ("e", "Fotos do passeio", "foto"),
                         ("Beatriz", "Chegamos às 12h então"), ("Mãe", "Perfeito. Não esqueçam de levar casaco"),
                         ("e", "Combinado 👍")],
    "Time do Projeto": [("Carlos Mendes", "Pessoal, reunião de alinhamento amanhã às 10h"),
                        ("Júlia Ferraz", "Confirmado. Já subi o relatório do trimestre", ("doc", "Relatorio_T3.pdf", 1_800_000)),
                        ("e", "Obrigado, Júlia! Vou revisar antes da reunião"),
                        ("Ana Souza", "Anexei também a planilha de custos", ("doc", "Custos_2026.pdf", 640_000)),
                        ("Carlos Mendes", "Ótimo. Pauta: prazos, orçamento e próximos passos"),
                        ("e", "Podemos incluir a revisão do cronograma?"),
                        ("Carlos Mendes", "Claro, a reunião vai ser objetiva")],
    "Carlos Mendes": [("o", "Conseguiu ver o contrato?"), ("e", "Vi sim, está tudo certo"),
                      ("o", "Então posso mandar para assinatura?"), ("e", "Pode! Obrigado pela agilidade")],
    "Clube de Leitura": [("Laura Campos", "Quem já terminou o capítulo 7?"), ("Marcos Teixeira", "Eu! Que final de capítulo"),
                         ("Laura Campos", "Trouxe as fotos do último encontro", "foto_pesada"),
                         ("e", "Ficaram ótimas! Obrigado por organizar"),
                         ("Marcos Teixeira", "E o mapa que desenhei para o próximo livro", "foto_pesada"),
                         ("Laura Campos", "Próxima reunião na quinta, às 19h")],
    "Júlia Ferraz": [("o", "Te mando o arquivo mais tarde"), ("e", "Sem pressa!")],
}

UMA_MENSAGEM = [("Pizzaria do Zé", "Seu pedido saiu para entrega! Chega em 15 minutos 🍕"),
                ("Banco Exemplo", "Código de verificação: 000000. Não compartilhe com ninguém."),
                (None, "Oi, aqui é da loja. Seu pedido está pronto para retirada.")]


def gerar_whatsapp(bk, rng):
    nomes = {"Ana Souza": 1, "Carlos Mendes": 2, "Beatriz": 3, "Mãe": 4, "Pai": 5, "Tio Roberto": 6, "Júlia Ferraz": 7,
             "Laura Campos": 11, "Marcos Teixeira": 12}
    jid_de = {n: f"{NUM(i)}@s.whatsapp.net" for n, i in nomes.items()}
    grupos = {"Grupo da Família": ("120363000000000001@g.us", ["Mãe", "Pai", "Tio Roberto", "Beatriz"]),
              "Time do Projeto": ("120363000000000002@g.us", ["Carlos Mendes", "Júlia Ferraz", "Ana Souza"]),
              "Clube de Leitura": ("120363000000000003@g.us", ["Laura Campos", "Marcos Teixeira", "Ana Souza"])}
    privados = ["Ana Souza", "Carlos Mendes", "Júlia Ferraz"]
    sessoes, mensagens, midias, membros, push = [], [], [], [], []
    pk_msg = pk_mid = pk_mem = 0
    sessao = 0

    def novo_membro(sess, nome):
        nonlocal pk_mem
        pk_mem += 1
        membros.append((pk_mem, sess, jid_de[nome], nome, nome.split()[0]))
        return pk_mem

    def add_midia(jid, tipo, rng_i, titulo=None, tamanho_doc=0):
        """Cria os arquivos da mídia e devolve (caminho, miniatura, tamanho, título)."""
        uid = f"{rng.getrandbits(128):032x}"
        base = f"Media/{jid}/{uid[0]}/{uid[1]}/{uid[:8]}-{uid[8:12]}-{uid[12:16]}-{uid[16:20]}-{uid[20:]}"
        if tipo == "doc":
            dados = pdf_falso(rng, tamanho_doc)
            bk.add(WA, f"Message/{base}.pdf", dados)
            return f"{base}.pdf", None, len(dados), titulo
        pesada = tipo == "foto_pesada"
        im = paisagem(rng, 2400, 1800, 0.10) if pesada else paisagem(rng, 1200, 900)
        dados = jpeg(im, 92 if pesada else 85)
        bk.add(WA, f"Message/{base}.jpg", dados)
        bk.add(WA, f"Message/{base}.thumb", jpeg(miniatura(im, 200), 70))
        return f"{base}.jpg", f"{base}.thumb", len(dados), None

    def conversa(nome, jid, grupo, integrantes):
        nonlocal sessao, pk_msg, pk_mid
        sessao += 1
        sess = sessao
        ids = {n: novo_membro(sess, n) for n in integrantes} if grupo else {}
        roteiro = ROTEIROS[nome]
        qtd_antigas = {"Grupo da Família": 110, "Time do Projeto": 80, "Clube de Leitura": 50, "Ana Souza": 40,
                       "Carlos Mendes": 14, "Júlia Ferraz": 0}[nome]
        # mensagens antigas (texto) + o roteiro final, do mais antigo para o mais novo
        antigas = []
        quem = integrantes or ["o"]
        for i in range(qtd_antigas):
            texto = rng.choice(FILLER)
            if i % 17 == 0:
                texto = "Pessoal, lembrem da reunião de quinta" if grupo else "Sobre a reunião, te explico depois"
            antigas.append((rng.choice(quem + ["e"]), texto, None))
        # antigas: espalhadas pelos 60 dias anteriores; roteiro: nos últimos minutos, de 6 em 6
        passo = dt.timedelta(days=58) / max(1, len(antigas))
        tempos = [FIM - dt.timedelta(days=60) + passo * i + dt.timedelta(minutes=rng.randint(0, 300)) for i in range(len(antigas))]
        tempos += [FIM - dt.timedelta(minutes=(len(roteiro) - i) * 6) for i in range(len(roteiro))]
        itens = antigas + [(r[0], r[1], r[2] if len(r) > 2 else None) for r in roteiro]
        ultima = None
        for i, (item, t) in enumerate(zip(itens, tempos)):
            quem_i, texto, extra = item
            roteiro_final = i >= len(antigas)
            eu = quem_i == "e"
            pk_msg += 1
            tipo, mid_pk = 0, None
            if extra:
                pk_mid += 1
                mid_pk = pk_mid
                if isinstance(extra, tuple):
                    caminho, thumb, tam, titulo = add_midia(jid, "doc", 0, extra[1], extra[2])
                    tipo = 8
                else:
                    caminho, thumb, tam, titulo = add_midia(jid, extra, 0)
                    tipo = 1
                midias.append((mid_pk, pk_msg, tam, caminho, thumb, titulo))
            membro = ids.get(quem_i) if grupo and not eu else None
            favorita = ("Obrigado" in texto) if roteiro_final else rng.random() < 0.015
            mensagens.append((pk_msg, sess, 1 if eu else 0, tipo, apple(t), texto, mid_pk, membro,
                              None if eu else jid, f"3EB0{rng.getrandbits(64):016X}", 1 if favorita else 0))
            ultima = (pk_msg, apple(t), texto)
        sessoes.append((sess, jid, nome, 1 if grupo else 0, ultima[0], ultima[1]))

    for nome, (jid, ints) in grupos.items():
        conversa(nome, jid, True, ints)
    for nome in privados:
        conversa(nome, jid_de[nome], False, [])
    for nome, texto in UMA_MENSAGEM:
        sessao += 1
        pk_msg += 1
        jid = f"{NUM(8 if nome and 'Pizza' in nome else 9 if nome else 10)}@s.whatsapp.net"
        t = FIM - dt.timedelta(days=rng.randint(3, 30))
        mensagens.append((pk_msg, sessao, 0, 0, apple(t), texto, None, None, jid, f"3EB0{rng.getrandbits(64):016X}", 0))
        sessoes.append((sessao, jid, nome, 0, pk_msg, apple(t)))
        if nome:
            push.append((jid, nome))

    for n, jid in jid_de.items():
        push.append((jid, n))

    def build(db):
        db.executescript("""
        create table ZWACHATSESSION (Z_PK integer primary key, Z_ENT integer, Z_OPT integer, ZARCHIVED integer, ZHIDDEN integer,
            ZREMOVED integer, ZSESSIONTYPE integer, ZLASTMESSAGE integer, ZLASTMESSAGEDATE timestamp,
            ZCONTACTJID varchar, ZPARTNERNAME varchar, ZLASTMESSAGETEXT varchar);
        create table ZWAMESSAGE (Z_PK integer primary key, Z_ENT integer, Z_OPT integer, ZISFROMME integer, ZMESSAGETYPE integer,
            ZSTARRED integer, ZCHATSESSION integer, ZGROUPMEMBER integer, ZMEDIAITEM integer, ZMESSAGEDATE timestamp,
            ZFROMJID varchar, ZSTANZAID varchar, ZTEXT varchar);
        create table ZWAMEDIAITEM (Z_PK integer primary key, Z_ENT integer, Z_OPT integer, ZFILESIZE integer, ZMOVIEDURATION integer,
            ZMESSAGE integer, ZLATITUDE float, ZLONGITUDE float, ZMEDIALOCALPATH varchar, ZTHUMBNAILLOCALPATH varchar,
            ZTITLE varchar, ZVCARDNAME varchar);
        create table ZWAGROUPMEMBER (Z_PK integer primary key, ZCHATSESSION integer, ZMEMBERJID varchar, ZCONTACTNAME varchar, ZFIRSTNAME varchar);
        create table ZWAPROFILEPUSHNAME (Z_PK integer primary key, ZJID varchar, ZPUSHNAME varchar);
        """)
        db.executemany("insert into ZWACHATSESSION (Z_PK, ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE, ZLASTMESSAGE, ZLASTMESSAGEDATE, ZREMOVED, ZARCHIVED, ZHIDDEN) "
                       "values (?,?,?,?,?,?,0,0,0)", sessoes)
        db.executemany("insert into ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGETYPE, ZMESSAGEDATE, ZTEXT, ZMEDIAITEM, ZGROUPMEMBER, ZFROMJID, ZSTANZAID, ZSTARRED) "
                       "values (?,?,?,?,?,?,?,?,?,?,?)", mensagens)
        db.executemany("insert into ZWAMEDIAITEM (Z_PK, ZMESSAGE, ZFILESIZE, ZMEDIALOCALPATH, ZTHUMBNAILLOCALPATH, ZTITLE) values (?,?,?,?,?,?)", midias)
        db.executemany("insert into ZWAGROUPMEMBER values (?,?,?,?,?)", membros)
        db.executemany("insert into ZWAPROFILEPUSHNAME (ZJID, ZPUSHNAME) values (?,?)", push)
    bk.add(WA, "ChatStorage.sqlite", sqlite_bytes(build))

    # fotos de perfil
    cores = {"Ana Souza": "#d9534f", "Carlos Mendes": "#5b8def", "Grupo da Família": "#2e9e6b", "Time do Projeto": "#8e5bd6"}
    alvo = {"Ana Souza": jid_de["Ana Souza"], "Carlos Mendes": jid_de["Carlos Mendes"],
            "Grupo da Família": grupos["Grupo da Família"][0], "Time do Projeto": grupos["Time do Projeto"][0]}
    for n, jid in alvo.items():
        bk.add(WA, f"Media/Profile/{jid.split('@')[0]}-1760000000.jpg", jpeg(avatar(n, cores[n])))
    return len(sessoes), len(mensagens)


# ---------------------------------------------------------------------------- Contatos
def gerar_contatos(bk, rng):
    HOME = "HomeDomain"
    pessoas = [
        # nome, sobrenome, org, cargo, aniversário, telefones, e-mails, endereço, foto
        ("Ana", "Souza", None, None, dt.datetime(1994, 5, 17), [("Mobile", "+55 00 91234-0001")], ["ana.souza@exemplo.com"], None, "#d9534f"),
        ("Carlos", "Mendes", "Estúdio Exemplo", "Diretor de projetos", None, [("Mobile", "+55 00 91234-0002"), ("Work", "+55 00 3333-0002")], ["carlos@estudioexemplo.com"], None, "#5b8def"),
        ("Beatriz", "Lima", None, None, dt.datetime(1998, 11, 3), [("Mobile", "+55 00 91234-0003")], [], None, "#2e9e6b"),
        ("Mãe", None, None, None, dt.datetime(1966, 2, 9), [("Mobile", "+55 00 91234-0004"), ("Home", "+55 00 2222-0004")], ["mae@exemplo.com"], ("Rua das Flores, 100", "Cidade Exemplo", "XX", "00000-000"), "#e0a800"),
        ("Pai", None, None, None, None, [("Mobile", "+55 00 91234-0005")], [], None, None),
        ("Roberto", "Lima", None, None, None, [("Mobile", "+55 00 91234-0006")], [], None, None),
        ("Júlia", "Ferraz", "Estúdio Exemplo", "Analista", dt.datetime(1991, 8, 22), [("Mobile", "+55 00 91234-0007")], ["julia@estudioexemplo.com"], None, None),
        (None, None, "Pizzaria do Zé", None, None, [("Work", "+55 00 3333-0008")], [], ("Av. Central, 50", "Cidade Exemplo", "XX", "00000-000"), None),
        ("Paulo", "Nunes", None, "Dentista", None, [("Work", "+55 00 3333-0020")], ["paulo.nunes@clinicaexemplo.com"], None, None),
        (None, None, "Banco Exemplo", None, None, [("Work", "+55 00 4004-0009")], [], None, None),
        ("Laura", "Campos", None, None, dt.datetime(1989, 3, 30), [("Mobile", "+55 00 91234-0011")], ["laura@exemplo.com"], None, None),
        ("Marcos", "Teixeira", None, None, None, [("Mobile", "+55 00 91234-0012")], [], None, None),
    ]
    rotulo = {"Mobile": "_$!<Mobile>!$_", "Home": "_$!<Home>!$_", "Work": "_$!<Work>!$_"}
    agora = apple(FIM)

    def build(db):
        db.executescript("""
        create table ABPerson (ROWID integer primary key, First text, Last text, Middle text, Organization text, Department text,
            Note text, Birthday real, JobTitle text, Nickname text, Prefix text, Suffix text, CreationDate real, ModificationDate real);
        create table ABMultiValue (UID integer primary key, record_id integer, property integer, identifier integer, label integer, value text);
        create table ABMultiValueLabel (ROWID integer primary key, value text);
        create table ABMultiValueEntryKey (ROWID integer primary key, value text);
        create table ABMultiValueEntry (parent_id integer, key integer, value text);
        """)
        for i, r in enumerate(["_$!<Mobile>!$_", "_$!<Home>!$_", "_$!<Work>!$_"], 1):
            db.execute("insert into ABMultiValueLabel values (?,?)", (i, r))
        for i, k in enumerate(["Street", "City", "State", "ZIP", "Country"], 1):
            db.execute("insert into ABMultiValueEntryKey values (?,?)", (i, k))
        uid = 0
        for pk, (nome, sob, org, cargo, aniv, tels, emails, ender, _) in enumerate(pessoas, 1):
            db.execute("insert into ABPerson values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pk, nome, sob, None, org, None, None, apple(aniv) if aniv else None, cargo, None, "Dr." if nome == "Paulo" else None,
                        None, agora - 86400 * 400, agora - 86400 * 20))
            for rot, tel in tels:
                uid += 1
                db.execute("insert into ABMultiValue values (?,?,?,?,?,?)", (uid, pk, 3, 0, ["_$!<Mobile>!$_", "_$!<Home>!$_", "_$!<Work>!$_"].index(rotulo[rot]) + 1, tel))
            for em in emails:
                uid += 1
                db.execute("insert into ABMultiValue values (?,?,?,?,?,?)", (uid, pk, 4, 0, 2, em))
            if ender:
                uid += 1
                db.execute("insert into ABMultiValue values (?,?,?,?,?,?)", (uid, pk, 5, 0, 2, None))
                for k, v in enumerate(ender + ("Brasil",), 1):
                    db.execute("insert into ABMultiValueEntry values (?,?,?)", (uid, k, v))
    bk.add(HOME, "Library/AddressBook/AddressBook.sqlitedb", sqlite_bytes(build))

    def imagens(db):
        db.execute("create table ABThumbnailImage (record_id integer, format integer, data blob)")
        db.execute("create table ABFullSizeImage (record_id integer, crop_x integer, data blob)")
        for pk, p in enumerate(pessoas, 1):
            if p[8]:
                nome = p[0] or p[2]
                db.execute("insert into ABThumbnailImage values (?,?,?)", (pk, 0, jpeg(avatar(nome, p[8], 120))))
                db.execute("insert into ABFullSizeImage values (?,?,?)", (pk, 0, jpeg(avatar(nome, p[8], 480))))
    bk.add(HOME, "Library/AddressBook/AddressBookImages.sqlitedb", sqlite_bytes(imagens))
    return len(pessoas)


# ---------------------------------------------------------------------------- Notas
NOTAS_DOM = "AppDomainGroup-group.com.apple.notes"
CONTA = "00000000-0000-4000-8000-0000000000AA"


def gerar_notas(bk, rng):
    uuid = lambda: "%08X-%04X-%04X-%04X-%012X" % (rng.getrandbits(32), rng.getrandbits(16), rng.getrandbits(16), rng.getrandbits(16), rng.getrandbits(48))
    # pastas: (pk, nome, tipo, pai)
    pastas = [(2, "Recently Deleted", 1, None), (3, "Notes", 0, None), (4, "Viagens", 0, None), (5, "Trabalho", 0, None), (6, "Reuniões", 0, 5)]
    anexos, midias_n, notas = [], [], []
    pk = [20]

    def novo():
        pk[0] += 1
        return pk[0]

    def imagem(nota_pk, nome_arq, im):
        a_pk, m_pk = novo(), novo()
        a_id, m_id = uuid(), uuid()
        bk.add(NOTAS_DOM, f"Accounts/{CONTA}/Media/{m_id}/{nome_arq}", jpeg(im, 85))
        anexos.append((a_pk, 4, nota_pk, m_pk, a_id, "public.jpeg"))
        midias_n.append((m_pk, 10, m_id, nome_arq))
        return a_id

    def nota(titulo, pasta, dias, montar, apagada=False):
        n_pk = novo()
        nb = Nota().linha(titulo, estilo=0)
        montar(nb, n_pk)
        data = nb.dados()
        t = FIM - dt.timedelta(days=dias)
        resumo = nb.texto.split("\n", 1)[1].strip().replace("￼", "")[:90] if "\n" in nb.texto else ""
        notas.append((n_pk, 11, titulo, resumo, pasta, apple(t - dt.timedelta(days=3)), apple(t), 1 if apagada else 0, data))

    def compras(nb, _):
        nb.linha("Para esta semana", estilo=1)
        for item, feito in [("Leite", True), ("Pão integral", True), ("Ovos", False), ("Tomate", False), ("Café", False)]:
            nb.linha(item, estilo=103, feito=feito)

    def ideias(nb, n_pk):
        nb.add("Objetivo: ", peso=1).linha("organizar e consultar o backup do celular sem depender de outro programa.")
        nb.linha("Funcionalidades", estilo=1)
        for it in ["Galeria de fotos por mês", "Conversas com busca e filtros", "Notas e contatos"]:
            nb.linha(it, estilo=100)
        nb.linha("Próximos passos", estilo=1)
        for it in ["Escrever o README", "Gerar dados de demonstração", "Publicar o código"]:
            nb.linha(it, estilo=102)
        nb.linha("")
        a = imagem(n_pk, "mapa-mental.jpg", paisagem(rng, 900, 600))
        nb.add("￼", att=(a, "public.jpeg")).linha("")
        nb.add("Dica: ", peso=3).linha("manter tudo local, sem enviar nada para a internet.")

    def bolo(nb, _):
        nb.linha("Ingredientes", estilo=1)
        for it in ["3 cenouras médias", "4 ovos", "1 xícara de óleo", "2 xícaras de açúcar", "2 xícaras de farinha", "1 colher de fermento"]:
            nb.linha(it, estilo=101)
        nb.linha("Modo de preparo", estilo=1)
        for it in ["Bata no liquidificador as cenouras, os ovos e o óleo.", "Misture com o açúcar e a farinha.",
                   "Acrescente o fermento e leve ao forno a 180 °C por 40 minutos."]:
            nb.linha(it, estilo=102)
        nb.add("Cobertura: ", peso=1).linha("chocolate derretido com um pouco de creme de leite.")

    def viagem(nb, n_pk):
        nb.linha("Checklist", estilo=1)
        for it, f in [("Passagens", True), ("Reserva do hotel", True), ("Passeio de barco", False), ("Seguro viagem", False)]:
            nb.linha(it, estilo=103, feito=f)
        nb.linha("")
        a = imagem(n_pk, "praia.jpg", paisagem(rng, 1000, 700))
        nb.add("￼", att=(a, "public.jpeg")).linha("")
        nb.linha("Chegada na quinta, às 14h. Volta no domingo à noite.")

    def ata(nb, _):
        nb.linha("Participantes: Carlos, Júlia, Ana e eu.")
        nb.linha("Decisões", estilo=1)
        nb.linha("Prazo da primeira entrega definido para o fim do mês.", estilo=100)
        nb.linha("Orçamento aprovado com ajuste de 5%.", estilo=100)
        nb.linha("Ações", estilo=1)
        for it, f in [("Revisar o cronograma", True), ("Enviar a proposta final", False), ("Marcar a próxima reunião", False)]:
            nb.linha(it, estilo=103, feito=f)

    def rascunho(nb, _):
        nb.linha("Ideias de presentes: livro, fone de ouvido, vale-café. Decidi não usar esta lista.")

    nota("Lista de compras", 3, 1, compras)
    nota("Ideias para o projeto", 5, 2, ideias)
    nota("Receita de bolo de cenoura", 3, 9, bolo)
    nota("Roteiro de viagem", 4, 14, viagem)
    nota("Ata da reunião 12/10", 6, 7, ata)
    nota("Rascunho: presentes de aniversário", 2, 30, rascunho, apagada=True)

    def build(db):
        db.executescript("""
        create table Z_PRIMARYKEY (Z_ENT integer primary key, Z_NAME varchar, Z_SUPER integer, Z_MAX integer);
        create table ZICCLOUDSYNCINGOBJECT (Z_PK integer primary key, Z_ENT integer, ZTITLE1 text, ZTITLE2 text, ZTITLE text,
            ZSNIPPET text, ZFOLDER integer, ZPARENT integer, ZFOLDERTYPE integer, ZCREATIONDATE3 real, ZMODIFICATIONDATE1 real,
            ZISPASSWORDPROTECTED integer, ZMARKEDFORDELETION integer, ZNOTE integer, ZMEDIA integer, ZIDENTIFIER text,
            ZTYPEUTI text, ZFILENAME text, ZPARENTATTACHMENT integer, ZURLSTRING text, ZALTTEXT text);
        create table ZICNOTEDATA (Z_PK integer primary key, ZNOTE integer, ZDATA blob);
        """)
        db.executemany("insert into Z_PRIMARYKEY (Z_ENT, Z_NAME) values (?,?)",
                       [(4, "ICAttachment"), (8, "ICInlineAttachment"), (10, "ICMedia"), (11, "ICNote"), (13, "ICAccount"), (14, "ICFolder")])
        for p_pk, nome, tipo, pai in pastas:
            db.execute("insert into ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZTITLE2, ZFOLDERTYPE, ZPARENT) values (?,14,?,?,?)", (p_pk, nome, tipo, pai))
        for n_pk, ent, titulo, resumo, pasta, criada, modif, apagada, data in notas:
            db.execute("insert into ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZTITLE1, ZSNIPPET, ZFOLDER, ZCREATIONDATE3, ZMODIFICATIONDATE1, "
                       "ZISPASSWORDPROTECTED, ZMARKEDFORDELETION) values (?,?,?,?,?,?,?,0,?)", (n_pk, ent, titulo, resumo, pasta, criada, modif, apagada))
            db.execute("insert into ZICNOTEDATA (ZNOTE, ZDATA) values (?,?)", (n_pk, data))
        for a_pk, ent, n_pk, m_pk, a_id, uti in anexos:
            db.execute("insert into ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZNOTE, ZMEDIA, ZIDENTIFIER, ZTYPEUTI) values (?,?,?,?,?,?)",
                       (a_pk, ent, n_pk, m_pk, a_id, uti))
        for m_pk, ent, m_id, nome in midias_n:
            db.execute("insert into ZICCLOUDSYNCINGOBJECT (Z_PK, Z_ENT, ZIDENTIFIER, ZFILENAME) values (?,?,?,?)", (m_pk, ent, m_id, nome))
    bk.add(NOTAS_DOM, "NoteStore.sqlite", sqlite_bytes(build))
    return len(notas)


# ---------------------------------------------------------------------------- Apps fictícios
def gerar_apps(bk, rng):
    apps = {
        "net.whatsapp.WhatsApp": ("WhatsApp Messenger", "W", "#25d366"),
        "com.exemplo.receitas": ("Receitas da Vovó", "R", "#e8743b"),
        "com.exemplo.corrida": ("Corrida Fácil", "C", "#3b82e8"),
        "com.exemplo.cofrinho": ("Cofrinho", "$", "#8e5bd6"),
    }
    info_apps = {}
    for bid, (nome, letra, cor) in apps.items():
        info_apps[bid] = {"iTunesMetadata": plistlib.dumps({"itemName": nome}), "PlaceholderIcon": png(icone_app(letra, cor)), "ApplicationSINF": b""}

    def receitas(db):
        db.execute("create table receitas (id integer primary key, titulo text, categoria text, tempo_min integer, created_date real, foto blob)")
        itens = [("Bolo de cenoura", "Sobremesa", 60), ("Feijoada leve", "Prato principal", 180), ("Salada de grão-de-bico", "Entrada", 20),
                 ("Pão caseiro", "Padaria", 120), ("Sopa de abóbora", "Entrada", 45)]
        for i, (t, c, m) in enumerate(itens, 1):
            db.execute("insert into receitas values (?,?,?,?,?,?)", (i, t, c, m, apple(FIM - dt.timedelta(days=i * 11)), jpeg(paisagem(rng, 160, 120), 70)))
    bk.add("AppDomain-com.exemplo.receitas", "Documents/receitas.sqlite", sqlite_bytes(receitas))
    bk.add("AppDomain-com.exemplo.receitas", "Library/Preferences/com.exemplo.receitas.plist",
           plistlib.dumps({"tema": "escuro", "ordenacao": "recentes", "receitas_favoritas": [1, 4], "ultimo_acesso": FIM}))

    # página com script dentro de um backup: o app nunca deve executá-la (usada nos testes de segurança)
    bk.add("AppDomain-com.exemplo.receitas", "Library/Caches/pagina.html",
           b"<!doctype html><title>x</title><script>document.title='executou'</script><p>conteudo de exemplo</p>")

    def corrida(db):
        db.execute("create table treinos (id integer primary key, data real, distancia_km real, duracao_s integer, ritmo text)")
        for i in range(14):
            km = round(rng.uniform(3, 12), 1)
            dur = int(km * rng.uniform(300, 380))
            db.execute("insert into treinos values (?,?,?,?,?)", (i + 1, apple(FIM - dt.timedelta(days=i * 2, hours=11)), km, dur,
                                                                   f"{dur // km // 60:.0f}:{dur / km % 60:02.0f}/km"))
    bk.add("AppDomain-com.exemplo.corrida", "Documents/treinos.sqlite", sqlite_bytes(corrida))
    bk.add("AppDomain-com.exemplo.corrida", "Library/Caches/ultima_rota.json",
           b'{"origem": "Parque Exemplo", "pontos": [[-0.001, -0.001], [-0.002, -0.0015], [-0.003, -0.002]], "km": 5.2}')

    # plist no formato NSKeyedArchiver, usado por muitos apps
    arquivo = {"$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"root": plistlib.UID(1)},
               "$objects": ["$null",
                            {"NS.keys": [plistlib.UID(2), plistlib.UID(3), plistlib.UID(4)],
                             "NS.objects": [plistlib.UID(5), plistlib.UID(6), plistlib.UID(7)], "$class": plistlib.UID(8)},
                            "meta_mensal", "saldo", "moeda", 750.0, 1234.56, "BRL",
                            {"$classname": "NSDictionary", "$classes": ["NSDictionary", "NSObject"]}]}
    bk.add("AppDomain-com.exemplo.cofrinho", "Library/Preferences/estado.plist", plistlib.dumps(arquivo, fmt=plistlib.FMT_BINARY))
    return info_apps


# ---------------------------------------------------------------------------- principal
def gerar(destino, seed=7):
    """Cria o backup fictício em destino/DEMO0000-0000000000000000 e devolve (caminho, estatísticas)."""
    rng = random.Random(seed)
    raiz = pathlib.Path(destino) / UDID
    if raiz.exists():
        shutil.rmtree(raiz)
    bk = Backup(raiz)
    stats = {"fotos": gerar_fotos(bk, rng)}
    stats["conversas"], stats["mensagens"] = gerar_whatsapp(bk, rng)
    stats["contatos"] = gerar_contatos(bk, rng)
    stats["notas"] = gerar_notas(bk, rng)
    apps = gerar_apps(bk, rng)
    bk.finish({"Device Name": "iPhone de Demonstração", "Product Type": "iPhone14,4", "Product Version": "17.0",
               "Unique Identifier": UDID, "Last Backup Date": FIM, "Applications": apps,
               "Installed Applications": list(apps)})
    return raiz, stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Gera um backup de iPhone fictício para demonstração e testes")
    ap.add_argument("destino", help="pasta onde criar o backup (ex.: demo)")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    caminho, st = gerar(a.destino, a.seed)
    print(f"Backup fictício criado em {caminho}\n  " + ", ".join(f"{v} {k}" for k, v in st.items()))
