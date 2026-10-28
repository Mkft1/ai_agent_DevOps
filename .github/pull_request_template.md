# Description

<!-- Что изменено и зачем. Ссылка на issue: Closes #... -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Documentation
- [ ] Refactoring / performance
- [ ] Security / guardrails

## Checklist

- [ ] Код проходит тесты (`make test`)
- [ ] Линтер не выдает ошибок (`make lint`)
- [ ] Обновлена документация (если применимо)
- [ ] Добавлены тесты (если применимо)

## How to verify

<!-- Команды/скриншоты для ревьюера: make eval, make dashboard, curl /diagnose ... -->

```bash
make lint && make test
```

## CI

- Пайплайн на PR: быстрые гейты (lint + helm параллельно → test c pytest-xdist);
  build/e2e едут только на `main`/workflow_dispatch.
- Для docs-only правок добавьте `[skip ci]` или `[ci skip]` в заголовок коммита.

## Notes for reviewers

<!-- Риски, rollback-план, связанные PR (stacked), миграции конфигов -->
