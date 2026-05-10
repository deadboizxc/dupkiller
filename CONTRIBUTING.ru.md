# Участие в разработке dupkiller

Спасибо за интерес к проекту!  Здесь описано, как настроить среду разработки,
запустить тесты и оформить изменения.

## Настройка среды разработки

```bash
git clone https://github.com/example/dupkiller
cd dupkiller
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Опционально — установить BLAKE3 для более быстрого хеширования при локальном тестировании:

```bash
pip install blake3
```

## Запуск тестов

```bash
# Все тесты с отчётом о покрытии
pytest --cov=dupkiller --cov-report=term-missing

# Один модуль
pytest tests/test_hashing.py -v
```

Покрытие должно оставаться на уровне 100%.  Новый код необходимо покрывать тестами.

## Линтинг и проверка типов

```bash
ruff check dupkiller tests   # линтинг
mypy dupkiller               # проверка типов
```

Оба инструмента должны завершаться без ошибок перед открытием pull request.

## Стиль коммитов

Используйте [Conventional Commits](https://www.conventionalcommits.org/):

```
<тип>(<область>): <краткое описание>

<тело — объясните что и почему, не как>
```

Типичные типы: `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `build`, `ci`.

Примеры:

```
feat(hashing): add xxHash fallback for environments without blake3

perf(cache): replace OFFSET pagination with keyset cursor

fix(scanner): skip broken symlinks instead of raising PermissionError
```

- Краткое описание — не более 72 символов.
- Тело объясняет *почему*, а не просто *что*.
- Ссылки на задачи оформляются как `Closes #123` в конце тела.

## Чек-лист pull request

- [ ] Все тесты проходят: `pytest --cov-fail-under=100`
- [ ] Нет ошибок линтера: `ruff check dupkiller tests`
- [ ] Нет ошибок типов: `mypy dupkiller`
- [ ] `CHANGELOG.md` и `CHANGELOG.ru.md` обновлены в секции `[Unreleased]`
- [ ] Новые публичные функции и классы имеют docstring в Google-стиле

## Сообщения об ошибках

Пожалуйста, укажите:

1. Версию dupkiller (`dupkiller --version`)
2. Версию Python и операционную систему
3. Минимальные шаги воспроизведения
4. Полный вывод ошибки (с флагом `--verbose`, если применимо)

## Лицензия

Отправляя изменения, вы соглашаетесь с тем, что они будут опубликованы под
лицензией [Apache 2.0](LICENSE).
