# Segurança

O Garimpo lida com dados muito pessoais (conversas, fotos, contatos), então relatos de segurança são levados a sério.

## Como relatar uma vulnerabilidade
Use a aba **Security → Report a vulnerability** deste repositório (relato privado). Por favor, **não abra uma issue pública** com detalhes de uma falha nem anexe dados reais de backup — se precisar reproduzir, use o backup fictício (`python tools/gerar_backup_demo.py demo`).

## O que o projeto já faz
O app escuta apenas em `127.0.0.1`, valida o cabeçalho `Host` e a `Origin`, usa uma política CSP restritiva, abre o backup somente para leitura, bloqueia SQL que não seja `SELECT` e nunca serve arquivos do backup como página. Os detalhes e os testes automáticos estão no [README](README.md#privacidade-e-segurança) e em `tests/test_demo.py`.
