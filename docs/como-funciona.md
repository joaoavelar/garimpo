# Como funciona

Este documento explica como um backup do iPhone é organizado e como o Garimpo lê cada tipo de dado. Nada aqui é documentação oficial da Apple: o formato foi entendido **inspecionando backups reais** e com base no conhecimento público da comunidade (veja as [referências](#referências)). Por isso pode mudar entre versões do iOS ou de apps.

## A pasta do backup

Cada backup é uma pasta com o identificador do aparelho (UDID):

```
Backup/
└── 00000000-0000000000000000/
    ├── Info.plist        nome do aparelho, modelo, versão do iOS, data, lista de apps instalados
    ├── Manifest.plist    entre outras coisas, se o backup está criptografado
    ├── Manifest.db       banco SQLite com o "índice" de todos os arquivos
    ├── 00/  01/  …  ff/  os arquivos de verdade, em 256 subpastas
    │     └── 31bb7ba8914766d4ba40d6dfb6113c8b614be442   (nome = SHA-1, veja abaixo)
    └── …
```

Os arquivos reais **não têm o nome original**. O nome é o `fileID`, e quem diz o que ele é o `Manifest.db`:

```sql
CREATE TABLE Files (fileID TEXT PRIMARY KEY, domain TEXT, relativePath TEXT, flags INTEGER, file BLOB);
```

| Coluna | Exemplo |
|---|---|
| `domain` | `HomeDomain`, `CameraRollDomain`, `AppDomain-com.exemplo.receitas`, `AppDomainGroup-group.net.whatsapp.WhatsApp.shared` |
| `relativePath` | `Library/AddressBook/AddressBook.sqlitedb` |
| `fileID` | `SHA-1("<domain>-<relativePath>")` em hexadecimal |
| `flags` | `1` = arquivo, `2` = pasta |
| `file` | um plist (`MBFile`) com tamanho, datas e permissões |

O arquivo físico fica em `<fileID[:2]>/<fileID>`. A regra do SHA-1 foi conferida em 2.000 arquivos sorteados de um backup real; é com ela que o gerador de backup fictício cria os arquivos.

```python
import hashlib
fid = hashlib.sha1("HomeDomain-Library/AddressBook/AddressBook.sqlitedb".encode()).hexdigest()
caminho = f"{fid[:2]}/{fid}"
```

O Garimpo carrega do `Manifest.db` o mapa `(domain, relativePath) → arquivo` e, a partir daí, "enxerga" o backup como se fosse o sistema de arquivos do celular. Os bancos são abertos com `immutable=1` (somente leitura, sem criar arquivos auxiliares).

> **Datas**: quase tudo usa segundos desde **1º/jan/2001** (a "época" da Apple), e não desde 1970. O Garimpo soma `978307200` para converter.

## Cada tipo de dado

### Fotos — `CameraRollDomain`
- `Media/PhotoData/Photos.sqlite`, tabela `ZASSET`: diretório e nome do arquivo (`ZDIRECTORY`, `ZFILENAME`), tipo (`ZKIND`: 0 foto, 1 vídeo), data, favorita, oculta, dimensões e duração.
- Original em `Media/<ZDIRECTORY>/<ZFILENAME>`; miniatura em `Media/PhotoData/Thumbnails/V2/<ZDIRECTORY>/<ZFILENAME>/5005.JPG`.
- Quem usa *Fotos do iCloud* "Otimizar armazenamento" tem miniaturas, mas não os originais — por isso algumas fotos aparecem esmaecidas.

### WhatsApp — `AppDomainGroup-group.net.whatsapp.WhatsApp.shared` (e `…WhatsAppSMB.shared` para o Business)
- `ChatStorage.sqlite` (Core Data):
  - `ZWACHATSESSION`: conversas (`ZCONTACTJID`, `ZPARTNERNAME`, `ZLASTMESSAGE`…). O `@g.us` no fim do JID indica grupo.
  - `ZWAMESSAGE`: mensagens (`ZTEXT`, `ZMESSAGEDATE`, `ZISFROMME`, `ZMESSAGETYPE`, `ZSTARRED`, `ZSTANZAID`…).
  - `ZWAMEDIAITEM`: anexos (`ZMEDIALOCALPATH`, `ZTHUMBNAILLOCALPATH`, `ZFILESIZE`…).
  - `ZWAGROUPMEMBER` e `ZWAPROFILEPUSHNAME`: nomes de quem participa dos grupos.
- Mídias: `Message/Media/<jid>/…`. Fotos de perfil: `Media/Profile/<número>-<data>.jpg`.
- Tipos de mensagem mais comuns: `0` texto, `1` foto, `2` vídeo, `3` áudio, `4` contato, `5` localização, `8` documento, `15` figurinha.
- A identidade de uma mensagem, para guardar exclusões e favoritas entre backups diferentes, é `JID da conversa + ZSTANZAID`.

### Notas — `AppDomainGroup-group.com.apple.notes`
- `NoteStore.sqlite`: uma tabela só (`ZICCLOUDSYNCINGOBJECT`) guarda pastas, notas, anexos e mídias; a coluna `Z_ENT` diz o tipo (a tabela `Z_PRIMARYKEY` traduz os números).
- O conteúdo de cada nota está em `ZICNOTEDATA.ZDATA`: **protobuf comprimido** (gzip ou zlib). Dentro vai o texto em UTF-16 e uma lista de "trechos" com estilo (título, negrito, lista, checklist…) e anexos (o caractere `U+FFFC` marca onde o anexo entra).
- Imagens ficam em `Accounts/<conta>/Media/<id da mídia>/<arquivo>`.
- O Garimpo traz um decodificador de protobuf mínimo, só com os campos que usa.

### Contatos — `HomeDomain`
- `Library/AddressBook/AddressBook.sqlitedb`: `ABPerson` (nome, empresa, aniversário…), `ABMultiValue` (telefones `property=3`, e-mails `4`, endereços `5`) e `ABMultiValueLabel` (rótulos como `_$!<Mobile>!$_`).
- Fotos em `AddressBookImages.sqlitedb`.

### Apps — `AppDomain-<bundle id>`
- O `Info.plist` do backup traz, para cada app instalado, o nome e o ícone; o Garimpo usa isso para nomear os domínios.
- Dentro de cada domínio vale o que o app guardou: bancos SQLite, plists (muitos em `NSKeyedArchiver`), JSON, imagens…

## Por que só backups sem criptografia?
Em backups criptografados os arquivos ficam cifrados com chaves derivadas da senha, e a Apple passa a incluir dados extras (histórico de chamadas, senhas salvas, Saúde). Decifrar exige a senha e a leitura do *keybag* do `Manifest.plist`. É possível — projetos como o `iphone_backup_decrypt` fazem isso — mas ainda não faz parte do Garimpo.

## Referências
- [libimobiledevice](https://github.com/libimobiledevice/libimobiledevice) — implementação aberta dos protocolos do iPhone, incluindo backup.
- [iLEAPP](https://github.com/abrignoni/iLEAPP) — extração forense de dados de apps iOS (esquemas de bancos).
- [iphone_backup_decrypt](https://github.com/jsharkey13/iphone_backup_decrypt) — leitura de backups criptografados.
