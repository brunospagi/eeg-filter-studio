# EEG Filter Studio

Ferramenta web para ajuste fino e validação de sinais EEG captados com o **OpenBCI Cyton** (8 canais), desenvolvida para uso no **Laboratório Sinapsense (UFPR)**.

Permite enviar um CSV exportado do OpenBCI, ajustar filtros interativamente (detrend, passa-banda, notch, detecção/mitigação de saturação do ADC), visualizar o sinal bruto x filtrado por canal, decompor em bandas clássicas de EEG (teta/alfa/beta/gama), comparar condições dentro de uma mesma gravação (olhos fechados x abertos) e comparar gravações diferentes entre si.

## Funcionalidades

- **Upload e cadastro de testes** — cada CSV enviado fica registrado permanentemente (nome, pessoa, sexo, idade, data), disponível para consulta e comparação depois, sem precisar reenviar o arquivo.
- **Filtros interativos** — remoção de offset/tendência, passa-banda Butterworth, notch (50/60 Hz + harmônicos), detecção de saturação do ADC com mitigação por interpolação. Cada filtro mostra a fórmula matemática por trás dele.
- **Modo Grade / Foco** — todos os canais empilhados ou um canal por vez, com sinal bruto e filtrado sobrepostos, saturação e trechos descartados marcados.
- **Modo Bandas** — decomposição em teta (4–8 Hz), alfa (8–13 Hz), beta (13–30 Hz) e gama (30–45 Hz) por canal, com potência RMS calculada excluindo a margem de acomodação do filtro.
- **Comparação olhos fechados x abertos** — para gravações marcadas como seguindo esse protocolo (5 marcadores), compara a potência por banda entre as duas condições dentro da mesma gravação.
- **Comparação entre testes** — seleciona 2 ou mais gravações já cadastradas (participantes/sessões diferentes) e compara potência por banda e sinal filtrado sobreposto, canal a canal.
- **Timeline/scrubber** — navega pela gravação inteira sincronizado com o gráfico principal.
- **Login obrigatório** — toda a aplicação exige autenticação (usuários Django padrão).

## Stack

Django 5 · NumPy/SciPy (processamento de sinal) · Pandas (parsing de CSV) · Plotly.js (gráficos interativos) · KaTeX (fórmulas) — sem banco de dados além do SQLite (usuários/sessão); as gravações ficam em `data/uploads/` como `.npz` + metadados em JSON.

## Rodando localmente

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

copy .env.example .env          # ajuste os valores conforme necessário

python manage.py migrate
python manage.py seed_admin     # cria o admin padrão (usuário/senha do .env)
python manage.py runserver
```

Acesse `http://localhost:8000`.

## Rodando com Docker

```bash
docker compose up --build
```

O `docker-entrypoint.sh` roda as migrations, coleta os arquivos estáticos e cria o admin padrão automaticamente a cada start (não sobrescreve se o usuário já existir).

**Usuário padrão:** `admin` / senha definida em `DJANGO_SUPERUSER_PASSWORD` no `.env` (padrão: `changeme123`). **Troque essa senha após o primeiro login** — é um valor conhecido, não um segredo real. Outros usuários podem ser criados em `/admin/`.

Os dados (banco SQLite + gravações cadastradas) persistem no volume `eeg-data`, independente de rebuild do container.

## Configuração (`.env`)

Veja `.env.example` para a lista completa. As principais:

| Variável | Descrição |
|---|---|
| `DJANGO_SECRET_KEY` | chave secreta do Django — gere uma nova para produção |
| `DJANGO_DEBUG` | `true` em dev, `false` em produção |
| `DJANGO_ALLOWED_HOSTS` | hosts permitidos, separados por vírgula |
| `DJANGO_SUPERUSER_USERNAME/PASSWORD/EMAIL` | conta admin criada automaticamente no primeiro start |

## Estrutura do projeto

```
eeg_web/            configuração do projeto Django (settings, urls)
signals/             app principal — parsing de CSV, filtros (dsp.py), views/API, templates, JS/CSS
signals/management/commands/seed_admin.py   cria o admin padrão
data/uploads/         gravações cadastradas (.npz + .json) — não versionado
```

> Scripts de análise offline em lote (validação com dados reais do laboratório, geração de figuras para o artigo científico) ficam em `analysis/` fora deste repositório — não são versionados aqui porque referenciam participantes da coleta por nome.

---

Uso interno — Laboratório Sinapsense, UFPR.
