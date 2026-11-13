# TV Online HD → Lista M3U para SS IPTV

Projeto completo que lê **todos os canais** do site
[https://www.tvonlinehd.app.br/](https://www.tvonlinehd.app.br/), testa se cada
transmissão está realmente tocando e publica uma lista **M3U** organizada por
categorias, atualizada automaticamente **a cada 6 horas** pelo GitHub Actions.

---

## Como funciona a coleta

O site é uma aplicação de página única: **não existe botão "carregar mais"**
e **não existe paginação**. Todas as categorias já vêm completas na página
inicial, então "carregar até o fim" equivale a abrir a página uma única vez
e ler tudo. A cada execução o projeto lê duas fontes e cruza as informações:

| Fonte | O que traz |
|---|---|
| Página inicial | cards de canal (nome, logo, número) e os **chips de categoria** |
| `player.html` | catálogo completo embutido (nome, logo, categorias e **link direto .m3u8**) |

Resultado: **580 canais**, distribuídos nas categorias do próprio site.

> O chip **Portugal** existe no site, mas neste momento está vazio (o site não
> associa nenhum canal a ele). Se o site passar a preencher, os canais aparecem
> automaticamente na próxima execução — nada precisa ser alterado.

---

## Regras de atualização (as que você pediu)

1. **Canal tocando** → continua na lista.
2. **Canal com falha** → ganha +1 falha e **permanece na lista**.
3. **Canal é removido só após 6 atualizações consecutivas com falha.**
4. Se o canal voltar a tocar antes disso, o contador **zera** e ele fica.
5. **Canais novos** encontrados no site entram automaticamente.
6. Canais que saíram do site são limpos do controle de estado.

O contador fica no arquivo `estado_canais.json`, que é salvo no repositório a
cada execução — por isso a regra das 6 falhas funciona mesmo com o GitHub
rodando de tempos em tempos (não é memória temporária).

---

## Como cada transmissão é testada

Duas etapas, em paralelo (24 canais ao mesmo tempo):

1. **HTTP** — o link responde `200` e o conteúdo é um manifesto HLS válido
   (`#EXTM3U`), descartando links mortos, redirecionamentos e páginas de erro.
2. **Vídeo real** — o `ffprobe` abre o stream e confirma que existe faixa de
   vídeo decodificável (codec e resolução). Só recebe esse teste quem passou na
   etapa 1, então a execução leva cerca de **1 minuto**.

No log final aparecem quantos canais estão tocando, quantos estão em falha, a
tabela por categoria e o tempo total.

---

## Otimizações para SS IPTV

- `group-title` = **categoria do site**, então no SS IPTV você navega por abas
  (Esportes, Filmes e Séries, Abertos, etc.).
- `tvg-logo` com a logo de cada canal (quando o site tem uma logo própria).
- `tvg-id` e `tvg-name` preenchidos.
- Canais ordenados por nome dentro de cada categoria.
- Uma entrada por canal — sem duplicar o mesmo link.
- Links apontam direto para o `.m3u8` (sem página intermediária), que é o que
  o SS IPTV e o VLC conseguem abrir.

---

## Instalação no GitHub (passo a passo)

1. Crie um repositório **público** no GitHub (ex.: `tvonlinehd-m3u`).
2. Envie todos os arquivos deste projeto para a branch `main`
   (incluindo a pasta `.github/workflows/atualizar.yml`).
3. Abra a aba **Actions** → *Atualizar lista M3U (TV Online HD)* →
   **Run workflow**. A primeira execução gera a lista e faz o commit sozinha.
4. Depois disso ela roda **de 6 em 6 horas** (00h, 06h, 12h, 18h UTC).
5. Pronto. O endereço da lista é:

```
https://raw.githubusercontent.com/SEU_USUARIO/tvonlinehd-m3u/main/tvonlinehd.m3u
```

### No SS IPTV

1. Abra o **SS IPTV** → *Configurações* → *Playlist externa*
   (ou pelo site [ss-iptv.com](https://ss-iptv.com) → *Content* → *External playlists*).
2. Cole o endereço `raw.githubusercontent.com/...` acima.
3. Se você alterou a lista, **remova e adicione a playlist de novo** para
   limpar o cache do aplicativo.

---

## Rodar no seu computador (Windows)

1. Instale o Python em [python.org](https://www.python.org/downloads/)
   marcando **"Add python.exe to PATH"**.
2. Recomendado: instale o ffmpeg em [gyan.dev](https://www.gyan.dev/ffmpeg/builds/)
   (versão *release full*) e coloque a pasta `bin` no PATH.
   Sem ele a validação fica só no HTTP.
3. Dê um duplo clique em **`rodar_no_brasil.bat`**.

Ao final será criado `tvonlinehd.m3u` na pasta do projeto.

### Comandos disponíveis

```bash
python atualizar_m3u.py                  # gera, testa e publica
python atualizar_m3u.py --sem-testar     # não valida os streams (rápido)
python atualizar_m3u.py --testar-tudo    # valida vídeo de todos os canais
python atualizar_m3u.py --iniciar-estado # zera os contadores de falha
python atualizar_m3u.py --limite-falhas 3  # muda a regra de remoção
```

Só usa a **biblioteca padrão** do Python — não precisa instalar pacotes com pip.

---

## Arquivos do projeto

| Arquivo | Para que serve |
|---|---|
| `atualizar_m3u.py` | script principal (coleta, teste, reconciliação e geração da lista) |
| `tvonlinehd.m3u` | **a lista** para usar no SS IPTV |
| `tvonlinehd_completo.m3u` | versão sem filtro (inclui os que estão em falha) |
| `estado_canais.json` | contador de falhas por canal (base da regra das 6 falhas) |
| `relatorio.json` | resumo da última execução (tocando, falhas, por categoria) |
| `log_ultima_execucao.txt` | log completo da última execução |
| `rodar_no_brasil.bat` | execução local no Windows |
| `.github/workflows/atualizar.yml` | automação das 6 horas |

---

## Observações importantes sobre a fonte

- Alguns servidores de vídeo (ex.: `tody.s25-cloudfront-net.lat` e
  `cdn10embed.xyz`) bloqueiam IPs fora do Brasil ou exigem o aplicativo
  oficial. Esses canais aparecem como **falha** na validação e **não entram**
  na lista principal — mas continuam no `tvonlinehd_completo.m3u` e no
  controle de estado, então **não são apagados por engano**; se o bloqueio
  deixar de existir, eles voltam sozinhos.
- O projeto **não reescreve** nenhum link: usa exatamente o endereço que o site
  publica. Não há proxy, nem servidor intermediário.
- Canais fechados (HBO, Telecine, Premiere, ESPN etc.) são os que o site
  disponibiliza publicamente; a disponibilidade pode mudar sem aviso.
- O script **nunca apaga a lista anterior** se o site estiver fora do ar: nesse
  caso ele registra o erro e mantém o arquivo que já existia.
