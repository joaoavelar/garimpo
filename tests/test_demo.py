"""Testes com o backup fictício (nenhum dado real).  Rodar:  python -m unittest discover -s tests -v"""
import hashlib
import http.client
import json
import pathlib
import sys
import tempfile
import threading
import unittest

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tools"))

import garimpo  # noqa: E402
import gerar_backup_demo  # noqa: E402


def arquivos(pasta):
    return sorted(p.relative_to(pasta).as_posix() for p in pathlib.Path(pasta).rglob("*"))


class BaseDemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = pathlib.Path(cls.tmp.name)
        cls.raiz, cls.stats = gerar_backup_demo.gerar(t / "backups")
        garimpo.STATE = garimpo.State(t / "dados" / "organizacao.json")
        cls.b = garimpo.Backup(cls.raiz)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestLeitura(BaseDemo):
    def test_resumo_do_backup(self):
        s = self.b.summary()
        self.assertFalse(s["encrypted"])
        self.assertEqual(s["apps"], ["wa"])
        self.assertTrue(s["notes"] and s["contacts"])
        self.assertEqual(s["name"], "iPhone de Demonstração")

    def test_fotos(self):
        fotos = self.b.photos()
        self.assertEqual(len(fotos), self.stats["fotos"])
        self.assertEqual(sum(not f["has"] for f in fotos), 5)  # 3 vídeos + 2 fotos "só no iCloud"
        self.assertEqual(sum(f["fav"] for f in fotos), 5)
        self.assertEqual(sum(f["video"] for f in fotos), 3)

    def test_whatsapp_pastas_automaticas(self):
        chats = {c["name"]: c for c in self.b.wa_chats("wa")["chats"]}
        self.assertEqual(len(chats), self.stats["conversas"])
        self.assertEqual(chats["Pizzaria do Zé"]["folder"], "inuteis")  # conversa de 1 mensagem
        self.assertEqual(chats["Grupo da Família"]["folder"], "grupos")
        self.assertEqual(chats["Ana Souza"]["folder"], "entrada")
        self.assertEqual(sum(c["folder"] == "inuteis" for c in chats.values()), 3)
        self.assertTrue(chats["Ana Souza"]["photo"])  # tem foto de perfil
        self.assertGreater(chats["Clube de Leitura"]["media_bytes"], 1_000_000)

    def test_busca_ignora_acentos_e_maiusculas(self):
        r = self.b.wa_search("wa", "REUNIAO")
        self.assertTrue(r["items"])
        self.assertTrue(all(i["hit"].lower() == "reunião" for i in r["items"]))

    def test_midias_ordenadas_por_tamanho(self):
        tam = [m["size"] for m in self.b.wa_media_list("wa", sort="size")["items"]]
        self.assertEqual(tam, sorted(tam, reverse=True))
        fotos = self.b.wa_media_list("wa", cat="foto")["total"]
        todas = self.b.wa_media_list("wa")["total"]
        self.assertGreater(todas, fotos > 0 and fotos)

    def test_contatos(self):
        c = garimpo.contacts(self.b)
        self.assertEqual(len(c), self.stats["contatos"])
        ana = next(x for x in c if x["name"] == "Ana Souza")
        self.assertEqual(ana["phones"][0]["label"], "Celular")
        self.assertEqual(ana["birthday"], "17/05/1994")
        self.assertTrue(garimpo.contact_photo(self.b, ana["id"]).startswith(b"\xff\xd8"))
        self.assertIn("BEGIN:VCARD", garimpo.vcard(self.b, c[:1]))

    def test_notas(self):
        idx = garimpo.notes_index(self.b)
        self.assertEqual(len(idx["notes"]), self.stats["notas"])
        htmls = {n["title"]: garimpo.note_detail(self.b, "/api/b/x", n["id"])["html"] for n in idx["notes"].values()}
        self.assertTrue(all("Não consegui" not in h for h in htmls.values()))
        self.assertIn("☑", htmls["Lista de compras"])                  # item marcado
        self.assertIn("☐", htmls["Lista de compras"])                  # item pendente
        self.assertIn("<img", htmls["Ideias para o projeto"])          # anexo de imagem
        self.assertIn("<ol>", htmls["Ideias para o projeto"])          # lista numerada
        self.assertIn("<b>Objetivo: </b>", htmls["Ideias para o projeto"])
        apagadas = [n for n in idx["notes"].values() if n["deleted"]]
        self.assertEqual(len(apagadas), 1)

    def test_explorador_de_apps(self):
        nomes = {e["name"] for e in garimpo.explorer_entries(self.b)}
        self.assertTrue({"Receitas da Vovó", "Corrida Fácil", "Cofrinho", "WhatsApp Messenger", "Notas"} <= nomes)
        dom = "AppDomain-com.exemplo.receitas"
        info = garimpo.explorer_info(self.b, dom, "Documents/receitas.sqlite")
        self.assertEqual(info["type"], "sqlite")
        rows = garimpo.explorer_rows(self.b, dom, "Documents/receitas.sqlite", "receitas")
        self.assertEqual(len(rows["rows"]), 5)
        # plist no formato NSKeyedArchiver vira um dicionário legível
        p = garimpo.explorer_info(self.b, "AppDomain-com.exemplo.cofrinho", "Library/Preferences/estado.plist")
        self.assertEqual(p["json"], {"meta_mensal": 750.0, "saldo": 1234.56, "moeda": "BRL"})


class TestNaoAltera(BaseDemo):
    def test_ler_tudo_nao_cria_nem_muda_arquivos_no_backup(self):
        antes = arquivos(self.raiz)
        sha = hashlib.sha1((self.raiz / "Manifest.db").read_bytes()).hexdigest()
        self.b.photos()
        self.b.wa_chats("wa")
        self.b.wa_search("wa", "obrigado")
        self.b.wa_media_list("wa")
        garimpo.contacts(self.b)
        garimpo.notes_index(self.b)
        garimpo.explorer_entries(self.b)
        garimpo.explorer_rows(self.b, "HomeDomain", garimpo.AB_DB, sql="select * from ABPerson")
        self.assertEqual(arquivos(self.raiz), antes)
        self.assertEqual(hashlib.sha1((self.raiz / "Manifest.db").read_bytes()).hexdigest(), sha)

    def test_excluir_so_oculta_no_app(self):
        chat = next(c for c in self.b.wa_chats("wa")["chats"] if c["name"] == "Ana Souza")
        alvo = self.b.file(garimpo.WA_DOMAINS["wa"], "ChatStorage.sqlite")
        sha = hashlib.sha1(alvo.read_bytes()).hexdigest()
        garimpo.STATE.apply("wa", {"op": "delete_chats", "chats": [chat["jid"]], "keep_starred": False})
        depois = next(c for c in self.b.wa_chats("wa")["chats"] if c["name"] == "Ana Souza")
        self.assertTrue(depois["deleted"])
        self.assertEqual(self.b.wa_messages("wa", chat["id"], mode="normal")["items"], [])   # some da conversa…
        self.assertTrue(self.b.wa_messages("wa", chat["id"], mode="all")["items"])           # …mas está na Lixeira
        self.assertEqual(hashlib.sha1(alvo.read_bytes()).hexdigest(), sha)  # banco do WhatsApp intocado
        garimpo.STATE.apply("wa", {"op": "restore_chats", "chats": [chat["jid"]]})

    def test_sql_so_de_leitura(self):
        for sql in ("delete from ABPerson", "drop table ABPerson", "attach database 'x.db' as x", "pragma writable_schema=1",
                    "update ABPerson set First='x'"):
            with self.assertRaises(ValueError, msg=sql):
                garimpo.explorer_rows(self.b, "HomeDomain", garimpo.AB_DB, sql=sql)

    def test_dados_pessoais_ficam_fora_do_projeto(self):
        self.assertNotIn(RAIZ, garimpo.default_data_dir().parents)


class TestServidor(BaseDemo):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        garimpo.Handler.backups = {cls.b.id: cls.b}
        cls.srv = garimpo.Server(("127.0.0.1", 0), garimpo.Handler)
        cls.porta = cls.srv.server_address[1]
        garimpo.Handler.allowed_hosts = {f"127.0.0.1:{cls.porta}", f"localhost:{cls.porta}"}
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        super().tearDownClass()

    def req(self, metodo, caminho, host=None, headers=None, corpo=None):
        c = http.client.HTTPConnection("127.0.0.1", self.porta, timeout=15)
        c.putrequest(metodo, caminho, skip_host=True)
        c.putheader("Host", host if host is not None else f"127.0.0.1:{self.porta}")
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        if corpo is not None:
            c.putheader("Content-Length", str(len(corpo)))
        c.endheaders(corpo)
        r = c.getresponse()
        dados = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, dados

    def test_host_estranho_e_recusado(self):
        for host in ("evil.example", f"evil.example:{self.porta}", "127.0.0.1:1", ""):
            self.assertEqual(self.req("GET", "/api/backups", host=host)[0], 403, host)
        self.assertEqual(self.req("GET", "/api/backups", host=f"localhost:{self.porta}")[0], 200)
        corpo = b'{"op": "folder_create", "name": "x"}'
        self.assertEqual(self.req("POST", "/api/wa/wa/action", host="evil.example",
                                  headers={"Content-Type": "application/json"}, corpo=corpo)[0], 403)

    def test_pagina_tem_cabecalhos_de_protecao(self):
        status, h, corpo = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("frame-ancestors 'none'", h["content-security-policy"])
        self.assertIn("default-src 'none'", h["content-security-policy"])
        self.assertEqual(h["x-content-type-options"], "nosniff")
        self.assertIn(b"Garimpo", corpo)
        self.assertNotIn(b"iBackup", corpo)

    def test_post_de_outro_site_e_recusado(self):
        corpo = b'{"op": "folder_create", "name": "x"}'
        ok = {"Content-Type": "application/json"}
        self.assertEqual(self.req("POST", "/api/wa/wa/action", headers={**ok, "Origin": "https://evil.example"}, corpo=corpo)[0], 403)
        self.assertEqual(self.req("POST", "/api/wa/wa/action", headers={"Content-Type": "text/plain"}, corpo=corpo)[0], 415)
        self.assertEqual(self.req("POST", "/api/wa/wa/action",
                                  headers={**ok, "Origin": f"http://127.0.0.1:{self.porta}"}, corpo=corpo)[0], 200)

    def test_html_do_backup_nunca_e_servido_como_pagina(self):
        q = "domain=AppDomain-com.exemplo.receitas&path=Library/Caches/pagina.html"
        status, h, _ = self.req("GET", f"/api/b/{self.b.id}/raw?{q}")
        self.assertEqual(status, 200)
        self.assertEqual(h["content-type"], "application/octet-stream")
        self.assertEqual(h["x-content-type-options"], "nosniff")
        for nome in ("a.html", "a.htm", "a.svg", "a.xml", "a.js"):
            self.assertEqual(garimpo.ctype_for(nome), "application/octet-stream", nome)

    def test_nao_ha_como_sair_da_pasta_do_backup(self):
        for caminho in ("../../../../Windows/win.ini", "..\\..\\Manifest.db", "/etc/passwd"):
            q = "domain=HomeDomain&path=" + caminho.replace("\\", "%5C")
            self.assertEqual(self.req("GET", f"/api/b/{self.b.id}/raw?{q}")[0], 404, caminho)
        self.assertEqual(self.req("GET", f"/api/b/{self.b.id}/explorer/blob?domain=HomeDomain&path={garimpo.AB_DB}"
                                         f"&table=x%22%3Bdrop&rowid=1&col=a")[0], 404)

    def test_ping_identifica_o_app(self):
        self.assertEqual(json.loads(self.req("GET", "/api/ping")[2]), {"app": "garimpo"})


if __name__ == "__main__":
    unittest.main()
