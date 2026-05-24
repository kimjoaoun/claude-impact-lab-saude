# Critérios de seleção, ordenamento, rankeamento e rerank de domicílios

Este documento descreve, em ordem de aplicação, como o app decide **quais domicílios entram na lista do dia**, **em que ordem aparecem**, e **como reagem a eventos do campo** (no-show, recusa, atendido na semana). Reflete o código atual em `app/streamlit_app.py` e `app/lib/data.py`.

> ⚠️ É uma heurística de prototipagem ("placeholder enquanto ranker real não existe"). Indicadores absolutos não são operacionais — ver `CLAUDE.md` sobre o efeito da anonimização nos dados.

---

## 1. Unidade de planejamento: o domicílio

A unidade não é o paciente, é o **domicílio sintético** (`id_familia_sint`). Definido em `app/lib/data.py:58` (`add_id_familia_sint`):

- grade territorial de **150m × 150m** sobre o território da equipe;
- chave = `fam_<equipe>_<cell_x>_<cell_y>`.

Cada domicílio agrega todos os pacientes que caem na mesma célula da grade dentro da mesma equipe. Toda lógica de ranking, seleção e status opera no nível do domicílio; status individual de cada morador continua existindo (`member_status`) e o status agregado da família é **derivado em runtime** a partir dos membros.

## 2. Panel do ACS: clusterização territorial

`clusterizar` (`app/lib/data.py:124`) particiona os pacientes da equipe em **K clusters** via `KMeans(lat, lon)`:

- `K = 8` por padrão (uma equipe = 8 ACS, ver memória `equipe-composicao`);
- mínimo de **20 pacientes por cluster** (`MIN_PACIENTES_POR_CLUSTER`), senão K é reduzido;
- `random_state = 42` (estabilidade entre runs).

O cluster selecionado pelo usuário é tratado como o panel hipotético de um ACS. **Todo o resto da pipeline opera dentro desse panel.**

## 3. Rankeamento de domicílios: faixa de prioridade

Função: `classify_family_priority` (`app/streamlit_app.py:184`). Para cada domicílio, soma um **score** com base em flags clínicas/sociais de seus membros (basta um membro ter a flag):

| Flag (qualquer membro)        | +score |
|-------------------------------|--------|
| gestação                      | 3      |
| vulnerabilidade (situação)    | 3      |
| diabético                     | 2      |
| hipertenso                    | 1      |
| pelo menos 1 evento clínico   | 1      |

Conversão para faixa (`priority_band`):

- score **≥ 4** → `alta`
- score **2–3** → `média`
- score **0–1** → `baixa`

`priority_member_ids` lista os membros do domicílio que individualmente disparam alguma flag (gestação / vulnerabilidade / diabético / evento) — usado depois pra detectar **gap de atendimento prioritário** (ver §6).

## 4. Ordenamento territorial: nearest-neighbor desde a UBS

Função: `lista_inicial` (`app/lib/data.py:136`).

A ordem-base **não é por prioridade**; é a sequência **nearest-neighbor planar** começando na UBS:

1. próximo domicílio = o de menor distância euclidiana (lat/lon) ao ponto atual;
2. anda a partir dele;
3. repete até esgotar.

Isso garante que a primeira render não venha aleatória e que a ordem reflita uma rota minimamente conexa. **Não é otimização de TSP** — só um varrer geográfico.

## 5. Seleção dos N domicílios do dia

Função: `selecao_dia` (`app/lib/data.py:200`). Recebe o panel territorialmente ordenado e produz uma lista de tamanho `N` (padrão `n_dia = 12`, slider 5–25).

A seleção monta a lista combinando **quatro pilhas em ordem de prioridade**, e dentro de cada pilha reordena por `priority_band` (alta → média → baixa):

```
forced_front  +  follow-ups  +  frescos  +  cooldown_tail
                                                          [:N]
```

| Pilha            | Quem entra                                                                                          |
|------------------|------------------------------------------------------------------------------------------------------|
| `forced_front`   | Reoferta forçada de alta prioridade da semana (§6).                                                  |
| `follow-ups`     | Domicílios cujo último status (snapshot anterior) é `não atendeu` ou `recusou atendimento`.          |
| `frescos`        | Resto do panel que **não** foi atendido nessa semana.                                                |
| `cooldown_tail`  | Domicílios atendidos em outro dia da mesma semana — vão para o fim.                                  |

Detalhes:

- **Cooldown semanal**: se um domicílio foi marcado `atendeu` em qualquer snapshot da semana corrente (`week_start` até ontem), entra no `cooldown_tail` (`app/streamlit_app.py:498-501`).
- **Forced front** (`app/streamlit_app.py:502-512`): domicílios de banda `alta` com snapshot da semana indicando `STATUS_NO_SHOW` **ou** "gap parcial de prioritário" (algum `priority_member_id` ficou sem ser atendido, mesmo que a família esteja marcada como atendida). Garante que casos altos não atendidos voltem para o topo.
- **Dentro de cada pilha** a ordem territorial (nearest-neighbor) é preservada, mas `_by_priority` puxa primeiro `alta`, depois `média`, depois `baixa`. Ou seja: **prioridade reordena dentro do bloco, sem misturar blocos**.

## 6. Rerank no-show (princípio)

Política durável (memória `rerank_no_show`):

> Paciente / domicílio **não atendido deve subir** na próxima lista priorizada — não voltar ao fim da fila.

Implementação:

- `prev_status`: o snapshot **mais recente anterior ao dia selecionado** (`latest_prior_snapshot`) define quem é `non-show` na próxima geração.
- Esses domicílios entram na pilha `follow-ups`, **acima** dos frescos.
- Se também forem `alta` da semana com gap, sobem para `forced_front` (topo absoluto).

## 7. Auto-substituto durante o dia (rerank em runtime)

Quando o ACS marca um domicílio como `não atendeu` ou `recusou atendimento` **durante o expediente**, o app procura um substituto fora da lista (`find_substitute_by_detour`, `app/lib/data.py:152`):

- **Pré-filtro de corredor**: candidato precisa estar a ≤ **300m** (`SUBSTITUTE_CORRIDOR_M`) de algum waypoint da rota restante;
- **Insere onde minimiza detour** (`d(prev,cand) + d(cand,next) - d(prev,next)`);
- **Limite duro**: detour ≤ **400m** (`SUBSTITUTE_MAX_DETOUR_M`);
- Se nenhum candidato qualifica, o trigger fica sem substituto e o slot é perdido.

A inserção é **posicional na ordem**, não em qualquer ponto: o substituto entra adjacente ao trecho onde o detour é mínimo.

## 8. Status agregado do domicílio (derivado, não armazenado)

Função: `derive_family_status` (`app/streamlit_app.py:59`). A partir do `member_status` de cada morador:

| Condição sobre os membros                                  | Status agregado            |
|------------------------------------------------------------|----------------------------|
| **Todos** os membros = `atendeu`                            | `atendeu`                  |
| **Pelo menos um** = `atendeu`, mas **não todos**            | `parcialmente atendeu`     |
| Algum = `recusou atendimento` (e nenhum atendido)           | `recusou atendimento`      |
| Algum = `não atendeu` (e nenhum atendido / recusado)        | `não atendeu`              |
| Caso contrário                                              | `pendente`                 |

O status familiar é **recalculado a cada render** e antes de cada autosave; não há ciclagem manual no nível do domicílio. `STATUS_PARCIAL` é apenas derivado — não entra em `STATUS_CYCLE` e não é selecionável no toggle individual.

`STATUS_REQUER_FOLLOWUP = {não atendeu, recusou atendimento}` — define quem dispara busca de substituto (§7) e quem aciona a pilha `follow-ups` (§5). `parcialmente atendeu` **não** dispara follow-up (parte da família foi atendida; rota não precisa de substituto).

## 9. Ordem final na UI (após gerar a lista)

A `ordem` exibida e usada para desenhar a rota OSRM é a saída de `selecao_dia` (§5), opcionalmente mutada por:

- **reordenação manual** do ACS na lista (drag/move na UI);
- **inserções de substituto** (§7).

OSRM recebe `coordinates = [UBS, ...ordem_filtrada]` com `?steps=false`, **sem `roundtrip`** e **sem otimização de waypoints**. A rota traçada respeita literalmente a ordem do ACS.

---

## Resumo em uma frase

> Para cada cluster territorial (proxy de ACS), o app monta a lista do dia varrendo o panel em nearest-neighbor desde a UBS, e reordena empurrando para o topo: (1) altas prioridades com no-show ou gap parcial recente, (2) follow-ups do dia anterior, e (3) ordenando cada bloco por banda `alta > média > baixa`; quem foi atendido na semana vai para o fim; durante o dia, no-shows disparam substituto off-list por detour mínimo dentro de um corredor de 300m.

## Arquivos críticos

- `app/lib/data.py` — `add_id_familia_sint`, `clusterizar`, `lista_inicial`, `selecao_dia`, `find_substitute_by_detour`.
- `app/streamlit_app.py` — `classify_family_priority`, `is_priority_member`, `weekly_family_outcomes`, `derive_family_status`, `build_family_panel`, geração de `forced_front_pids` / `cooldown_pids` (linhas ~485-521).
- `app/lib/state.py` — `latest_prior_snapshot`, `snapshots_in_range` (insumos para rerank e cooldown).
