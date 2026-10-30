#!/usr/bin/env bash
#
# verify_ci_cache.sh — эмпирическая проверка стабильности кэширования в CI.
#
# Назначение
# ----------
# После оптимизаций пайплайна (actions/cache@v6 для pip с единым ключом на все
# Python-джобы; cache-from/cache-to type=gha,mode=max для BuildKit) нужно
# воспроизводимо подтверждать, что:
#   * кэш pip стабильно попадает (cache hit) в джобах lint/test/dependency-audit;
#   * Docker-сборка импортирует кэш-манифест BuildKit из GHA-кэша;
#   * независимые джобы (lint, helm) реально стартуют параллельно;
#   * в Annotations нет предупреждений (0 warnings / 0 notices);
#   * полный ран на main укладывается в целевые 5-8 минут;
#   * квота кэшей (10 GB на репозиторий) не «пухнет» и нет race на запись ключей.
#
# Что делает скрипт
# -----------------
#   1. Дважды запускает workflow CI через workflow_dispatch:
#        gh workflow run CI --ref main -f reason="cache-verification"
#      с интервалом 10 секунд (INTERVAL настраивается).
#   2. Печатает URL двух последних запусков:
#        gh run list --limit 2 --json databaseId,url -q '.[].url'
#   3. Печатает чек-лист ручной проверки в UI GitHub Actions (в конце).
#   4. Опционально (AUTO_VERIFY=1) сам прогоняет чек-лист через gh api:
#      тайминги шагов, строки «Cache restored successfully» / «importing cache
#      manifest» из логов, аннотации, параллельность lint/helm, total time,
#      размер кэшей до/после.
#
# ВАЖНО: concurrency и «отменённый» первый ран
# --------------------------------------------
# В ci.yml задано:
#     concurrency:
#       group: ${{ github.workflow }}-${{ github.ref }}
#       cancel-in-progress: true
# Группа для dispatch на main одна (CI-refs/heads/main), поэтому ВТОРОЙ
# запуск через 10 секунд ОТМЕНИТ первый (status=cancelled). Это штатная
# защита от race-состояния при записи одних и тех же кэш-ключей, а не баг.
# Чек-лист в таком режиме проверяйте по второму (завершённому) рану.
# Если нужны ДВА ПОЛНЫХ рана (сравнение hit-rate между запусками подряд):
#     SEQUENTIAL=1 ./scripts/verify_ci_cache.sh   # ждать завершения каждого
#
# Переменные окружения (все опциональны)
# --------------------------------------
#   REPO=Mkft1/ai_agent_DevOps  целевой репозиторий (owner/name)
#   WORKFLOW=CI                 workflow (название или имя файла)
#   INTERVAL=10                 пауза между dispatch, сек
#   RUNS=2                      количество запусков
#   SEQUENTIAL=1                последовательный режим (без отмены первого рана)
#   AUTO_VERIFY=1               автоматическая проверка чек-листа через gh api
#
# Требования
# ----------
#   gh CLI, успешный `gh auth status`. Права токена: actions:write (dispatch),
#   actions:read (раны/логи/кэши), checks:read (аннотации).
#
# Примечание про `-f reason=...`
# ------------------------------
# GitHub ОТВЕРГАЕТ dispatch с ключами, которые не объявлены в workflow:
# 422 "Unexpected inputs provided: [reason]" (проверено эмпирически 2026-10-30
# против ci.yml без секции inputs). Поэтому в этом же PR в ci.yml добавлено:
#     on:
#       workflow_dispatch:
#         inputs:
#           reason:
#             description: "Причина ручного запуска"
#             type: string
#             required: false
#             default: "manual"
# После этого dispatch с reason=cache-verification проходит (204), а reason
# виден на странице рана в секции "Workflow inputs".

set -euo pipefail

REPO="${REPO:-Mkft1/ai_agent_DevOps}"
WORKFLOW="${WORKFLOW:-CI}"
INTERVAL="${INTERVAL:-10}"
RUNS="${RUNS:-2}"

C_BLUE='\033[1;34m'; C_GREEN='\033[0;32m'; C_YELLOW='\033[1;33m'; C_RED='\033[0;31m'; C_RESET='\033[0m'
log()  { printf "${C_BLUE}==>${C_RESET} %s\n" "$*"; }
ok()   { printf "${C_GREEN}[OK]${C_RESET}   %s\n" "$*"; }
warn() { printf "${C_YELLOW}[WARN]${C_RESET} %s\n" "$*"; }
die()  { printf "${C_RED}[ERROR]${C_RESET} %s\n" "$*" >&2; exit 1; }

# ─────────────────────────────────────────────────────────────────────────────
# Чек-лист: что смотреть в UI (печатается всегда, даже если gh недоступен)
# ─────────────────────────────────────────────────────────────────────────────
print_checklist() {
  cat <<CHECKLIST
──────────────────────────────────────────────────────────────────────────────
ЧЕК-ЛИСТ ПРОВЕРКИ — UI GitHub Actions → Actions → два последних рана CI
──────────────────────────────────────────────────────────────────────────────
[ ] 1. Шаг "Cache pip dependencies" в джобах lint, test, dependency-audit
       показывает статус "Cache hit" (зелёная галочка, время restore ~5-15 s).
       В логе шага:      "Cache restored successfully"
       В Post-шаге:      "Cache hit occurred on the primary key Linux-pip-...,
                          not saving cache."
       Примечание: фактическое время restore пропорционально объёму кэша
       (~3 GB pip-кэша восстанавливаются ~30-40 с; 5-15 с — оценка для кэша
       порядка 1 GB).

[ ] 2. Шаг "Build image" использует кэш BuildKit. В логах шага:
         "importing cache manifest from gha:<id>"   (импорт кэша)
         слои с пометкой "CACHED"
       В Post-шаге — экспорт ("exporting cache ..."), для неизменившихся
       слоёв — пропуск. Ключевые признаки race при конкурентных сборках:
         "Unable to reserve cache with key ..."  — не должно появляться
       при последовательных запусках.

[ ] 3. Джобы lint и helm стартуют параллельно: на странице рана время начала
       (started_at) отличается не более чем на 2-3 секунды.

[ ] 4. Раздел "Annotations" вверху страницы рана пуст: 0 warnings, 0 notices.

[ ] 5. Общее время выполнения рана — в диапазоне 5-8 минут
       (created_at → updated_at в API, либо длительности джоб в UI).

Дополнительно (квоты / стабильность между запусками):
[ ] 6. Actions → Caches: суммарный размер не растёт от рана к рану.
       Ключ Linux-pip-<hash> сохраняется ОДИН раз; при cache-hit повторный
       save пропускается ("not saving cache") — гонки за запись ключа нет.
       Квота репозитория — 10 GB; при достижении GitHub вытесняет LRU.
[ ] 7. При запуске по ТЗ (2 dispatch с интервалом 10 с) ПЕРВЫЙ ран ожидаемо
       получит статус cancelled — это сработала concurrency-защита ci.yml
       (cancel-in-progress: true). Для двух полных ранов: SEQUENTIAL=1.
──────────────────────────────────────────────────────────────────────────────
Ручная проверка без UI (тот же чек-лист через gh):
  gh api repos/$REPO/actions/runs/<RUN_ID>/jobs --jq '.jobs[] | {name, started_at, conclusion}'
  gh run view <RUN_ID> --log | grep -E "Cache restored successfully|Cache hit occurred|importing cache manifest"
  gh api repos/$REPO/actions/caches --jq '{count: .total_count, bytes: .total_size}'
──────────────────────────────────────────────────────────────────────────────
CHECKLIST
}

# ─────────────────────────────────────────────────────────────────────────────
# Пре-флайт: gh доступен и аутентифицирован?
# ─────────────────────────────────────────────────────────────────────────────
if ! command -v gh >/dev/null 2>&1; then
  warn "gh CLI не найден — скрипт не может сам запустить workflow."
  echo   "  Установка: https://cli.github.com/  (brew install gh | winget install GitHub.cli | apt install gh)"
  echo   "  Логин:     gh auth login   (нужны права actions:write)"
  echo
  echo   "Ручной эквивалент шагов скрипта:"
  echo     "  gh workflow run $WORKFLOW --repo $REPO --ref main -f reason=\"cache-verification\""
  echo     "  sleep $INTERVAL"
  echo     "  gh workflow run $WORKFLOW --repo $REPO --ref main -f reason=\"cache-verification\""
  echo     "  gh run list --repo $REPO --workflow $WORKFLOW --event workflow_dispatch --limit 2 --json databaseId,url -q '.[].url'"
  echo
  print_checklist
  exit 1
fi
gh auth status >/dev/null 2>&1 || die "gh не аутентифицирован — выполните 'gh auth login'"

# Текущее состояние кэшей — «до» (для сравнения «после» в AUTO_VERIFY).
log "Кэши Actions ДО запуска (квота 10 GB):"
BEFORE_SIZE="$(gh api "repos/$REPO/actions/caches" --jq '.total_size' 2>/dev/null || echo '')"
if [ -n "$BEFORE_SIZE" ]; then
  gh api "repos/$REPO/actions/caches" --jq '"  entries: \(.total_count), total: \(.total_size) bytes (~\( (.total_size/1073741824*100|floor)/100 ) GB)"' \
    || warn "не удалось прочитать список кэшей (нужен actions:read)"
else
  warn "список кэшей недоступен — продолжаю без сравнения квоты"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 1) Запуск workflow (RUNS раз)
# ─────────────────────────────────────────────────────────────────────────────
latest_dispatch_id() {
  # ID последнего dispatch-рана CI на main (может быть пустым, если ранов ещё нет)
  gh run list --repo "$REPO" --workflow "$WORKFLOW" --event workflow_dispatch \
     --branch main --limit 1 --json databaseId --jq '.[0].databaseId // empty' 2>/dev/null || true
}

TRIGGERED_IDS=()
for i in $(seq 1 "$RUNS"); do
  before_id="$(latest_dispatch_id)"
  log "Dispatch #$i/$RUNS: gh workflow run $WORKFLOW --ref main -f reason=\"cache-verification\""
  gh workflow run "$WORKFLOW" --repo "$REPO" --ref main -f reason="cache-verification"

  # Ждём регистрации нового рана (dispatch создаёт run асинхронно, до ~60 с).
  new_id=""
  for _ in $(seq 1 30); do
    sleep 2
    cur="$(latest_dispatch_id)"
    if [ -n "$cur" ] && [ "$cur" != "$before_id" ]; then new_id="$cur"; break; fi
  done
  if [ -n "$new_id" ]; then
    TRIGGERED_IDS+=("$new_id")
    ok "ран #$i зарегистрирован: id=$new_id  https://github.com/$REPO/actions/runs/$new_id"
  else
    warn "ран #$i не зарегистрировался за 60 с — проверьте вкладку Actions вручную"
  fi

  if [ "$i" -lt "$RUNS" ]; then
    if [ "${SEQUENTIAL:-0}" = "1" ]; then
      # Последовательный режим: ждём ЗАВЕРШЕНИЯ рана, иначе следующий dispatch
      # его отменит (concurrency cancel-in-progress в ci.yml).
      [ -n "$new_id" ] || die "SEQUENTIAL=1, но id рана неизвестен — прерываюсь"
      log "SEQUENTIAL: жду завершения рана $new_id ..."
      gh run watch "$new_id" --repo "$REPO" --interval 15 --exit-status >/dev/null 2>&1 \
        || warn "ран $new_id завершился не успехом (cancelled/failure) — см. UI"
    else
      sleep "$INTERVAL"
    fi
  fi
done

# ─────────────────────────────────────────────────────────────────────────────
# 2) Ссылки на запущенные ранги (формат из ТЗ + статусная таблица)
# ─────────────────────────────────────────────────────────────────────────────
sleep 3  # даём API окончательно зарегистрировать оба рана перед выборкой
log "Ссылки на два последних dispatch-рана (как в ТЗ):"
gh run list --repo "$REPO" --workflow "$WORKFLOW" --event workflow_dispatch --branch main \
  --limit 2 --json databaseId,url -q '.[].url'

log "Статусы:"
gh run list --repo "$REPO" --workflow "$WORKFLOW" --event workflow_dispatch --branch main \
  --limit "$RUNS" --json databaseId,status,conclusion,createdAt \
  --jq '.[] | "  run \(.databaseId)  \(.status)/\(.conclusion // "-")  created \(.createdAt)"' || true

if [ "${SEQUENTIAL:-0}" != "1" ]; then
  warn "concurrency(cancel-in-progress: true) в ci.yml: второй dispatch, скорее всего,"
  warn "отменит первый (status=cancelled) — это штатная защита от race при записи кэшей."
  warn "Для двух ПОЛНЫХ ранов сравнения используйте: SEQUENTIAL=1 $0"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 3) AUTO_VERIFY: автоматический прогон чек-листа через gh api (опционально)
#    Каждый пункт печатает [OK]/[WARN] с фактическими числами. Best-effort:
#    любая ошибка запроса деградирует до [WARN], скрипт не падает.
# ─────────────────────────────────────────────────────────────────────────────
epoch_of() {  # ISO-8601 UTC -> unix time (нужен GNU date: Linux/WSL; на macOS BSD date вернёт пусто)
  date -u -d "$1" +%s 2>/dev/null || true
}

mmss_of() {  # секунды -> "Xm Ys"
  printf '%dm %ds' $(( $1 / 60 )) $(( $1 % 60 ))
}

verify_run() {
  local id="$1"
  log "──── AUTO_VERIFY рана $id ────"
  local meta sha status concl created updated
  meta="$(gh api "repos/$REPO/actions/runs/$id" \
    --jq '[.head_sha, .status, (.conclusion // "-"), .created_at, .updated_at] | @tsv' 2>/dev/null || true)"
  if [ -z "$meta" ]; then warn "не удалось прочитать метаданные рана $id"; return 0; fi
  IFS=$'\t' read -r sha status concl created updated <<<"$meta"
  echo "  sha=$sha status=$status conclusion=$concl"

  if [ "$status" != "completed" ]; then
    warn "ран ещё не завершён (status=$status) — запускайте AUTO_VERIFY позже"
    return 0
  fi
  if [ "$concl" = "cancelled" ]; then
    warn "ран ОТМЕНён (conclusion=cancelled). Ожидаемо для первого dispatch при"
    warn "интервале 10 с: сработала concurrency-защита ci.yml (cancel-in-progress)."
    return 0
  fi

  # [5] Total time
  local c_e u_e total
  c_e="$(epoch_of "$created")"; u_e="$(epoch_of "$updated")"
  if [ -n "$c_e" ] && [ -n "$u_e" ]; then
    total=$(( u_e - c_e ))
    if [ "$total" -ge 300 ] && [ "$total" -le 480 ]; then
      ok "[5] Total time $(mmss_of "$total") — в целевом диапазоне 5-8 мин"
    else
      warn "[5] Total time $(mmss_of "$total") — вне диапазона 5-8 мин (см. джобы ниже)"
    fi
  else
    warn "[5] Total time: нужен GNU date; created=$created updated=$updated (посчитайте разницу)"
  fi

  # [1]+[3] Джобы: параллельность lint/helm и шаги "Cache pip dependencies"
  local jobs_tsv lint_start helm_start ls_e hs_e diff
  jobs_tsv="$(gh api "repos/$REPO/actions/runs/$id/jobs" --paginate --jq '
      .jobs[]
      | [ .name, .conclusion, .started_at,
          ( [.steps[] | select(.name=="Cache pip dependencies") | "\(.conclusion) \(.started_at)→\(.completed_at)"] | join("; ") ) ]
      | @tsv' 2>/dev/null || true)"
  if [ -n "$jobs_tsv" ]; then
    echo "  Джобы (name | conclusion | started_at | шаг 'Cache pip dependencies'):"
    while IFS=$'\t' read -r jn jc js cache_step; do
      printf '    %-42s %-10s %s   %s\n' "$jn" "$jc" "$js" "${cache_step:--}"
      case "$jn" in
        "Lint (ruff)")            lint_start="$js" ;;
        "Helm lint & template")   helm_start="$js" ;;
      esac
    done <<<"$jobs_tsv"
    ls_e="$(epoch_of "${lint_start:-}")"; hs_e="$(epoch_of "${helm_start:-}")"
    if [ -n "$ls_e" ] && [ -n "$hs_e" ]; then
      diff=$(( ls_e > hs_e ? ls_e - hs_e : hs_e - ls_e ))
      if [ "$diff" -le 3 ]; then
        ok "[3] lint и helm стартовали параллельно (разница ${diff} с ≤ 3 с)"
      else
        warn "[3] lint/helm стартовали с разницей ${diff} с (> 3 с) — проверьте 'needs' в ci.yml"
      fi
    else
      warn "[3] параллельность lint/helm: сравните started_at выше вручную (нужен GNU date)"
    fi
  else
    warn "[1]/[3] не удалось прочитать список джоб рана"
  fi

  # [1]/[2]/[6] Доказательства из логов: cache-hit, BuildKit-кэш, признаки race
  local logs
  logs="$(gh run view "$id" --repo "$REPO" --log 2>/dev/null || true)"
  if [ -n "$logs" ]; then
    local n_hits n_build n_race
    n_hits="$(printf '%s\n' "$logs" | grep -c 'Cache hit occurred on the primary key Linux-pip-' || true)"
    n_build="$(printf '%s\n' "$logs" | grep -c 'importing cache manifest from gha' || true)"
    n_race="$(printf '%s\n' "$logs" | grep -c 'Unable to reserve cache' || true)"
    if [ "$n_hits" -ge 3 ]; then
      ok "[1] pip-кэш: '$n_hits' подтверждений 'Cache hit occurred ... not saving cache' (ждём ≥3: lint/test/dependency-audit)"
    else
      warn "[1] pip-кэш: найдено только '$n_hits' подтверждений cache-hit (ожидали ≥3) — возможен cache miss"
    fi
    if [ "$n_build" -ge 1 ]; then
      ok "[2] BuildKit: '$n_build' строк 'importing cache manifest from gha' в логах сборки"
    else
      warn "[2] BuildKit: строка 'importing cache manifest' не найдена — сборка, возможно, шла без кэша"
    fi
    if [ "$n_race" -eq 0 ]; then
      ok "[6] race-признаков нет: 'Unable to reserve cache' — 0 вхождений"
    else
      warn "[6] найдено '$n_race' вхождений 'Unable to reserve cache' — конкурентная запись кэш-ключей (race)"
    fi
  else
    warn "[1]/[2]/[6] логи рана недоступны (gh run view --log) — проверьте шаги в UI"
  fi

  # [4] Annotations рана (по check-suite именно этого рана)
  local csid cr_ids ann_warn ann_notice cr a
  csid="$(gh api "repos/$REPO/actions/runs/$id" --jq '.check_suite_id' 2>/dev/null || true)"
  if [ -n "$csid" ]; then
    ann_warn=0; ann_notice=0
    cr_ids="$(gh api "repos/$REPO/check-suites/$csid/check-runs" --paginate --jq '.check_runs[].id' 2>/dev/null || true)"
    for cr in $cr_ids; do
      a="$(gh api "repos/$REPO/check-runs/$cr/annotations" --paginate --jq '[.[] | select(.annotation_level=="warning")] | length' 2>/dev/null || echo 0)"
      ann_warn=$(( ann_warn + ${a:-0} ))
      a="$(gh api "repos/$REPO/check-runs/$cr/annotations" --paginate --jq '[.[] | select(.annotation_level=="notice")] | length' 2>/dev/null || echo 0)"
      ann_notice=$(( ann_notice + ${a:-0} ))
    done
    if [ "$ann_warn" -eq 0 ] && [ "$ann_notice" -eq 0 ]; then
      ok "[4] Annotations: 0 warnings, 0 notices"
    else
      warn "[4] Annotations: warnings=$ann_warn notices=$ann_notice (ждём 0/0)"
    fi
  else
    warn "[4] не удалось получить check_suite_id рана — посмотрите Annotations в UI"
  fi
}

verify_quota() {
  local after
  after="$(gh api "repos/$REPO/actions/caches" --jq '"\(.total_count) \(.total_size)"' 2>/dev/null || true)"
  if [ -n "$after" ]; then
    # shellcheck disable=SC2086  # намеренное разбиение "count bytes" на $1/$2
    set -- $after
    log "Кэши Actions ПОСЛЕ: entries=$1 total=$2 bytes"
    if [ -n "$BEFORE_SIZE" ]; then
      local delta=$(( $2 - BEFORE_SIZE ))
      if [ "$delta" -le 0 ]; then
        ok "[6] квота не превышена и не растёт: дельта ${delta} bytes (до=$BEFORE_SIZE)"
      else
        warn "[6] объём кэшей вырос на ${delta} bytes (до=$BEFORE_SIZE) — проверьте новые ключи в Actions → Caches"
      fi
    fi
  fi
}

if [ "${AUTO_VERIFY:-0}" = "1" ]; then
  for id in ${TRIGGERED_IDS[@]+"${TRIGGERED_IDS[@]}"}; do
    log "Ожидание завершения рана $id (может занять 5-10 минут) ..."
    gh run watch "$id" --repo "$REPO" --interval 15 >/dev/null 2>&1 || true
    verify_run "$id"
  done
  verify_quota
fi

# ─────────────────────────────────────────────────────────────────────────────
# 4) Чек-лист ручной проверки (печатается всегда — по ТЗ «после запуска»)
# ─────────────────────────────────────────────────────────────────────────────
print_checklist
