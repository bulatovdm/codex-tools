# Журнал

## 2026-10-14

- Собран отдельный репозиторий по просьбе Дмитрия, рядом с `claude-tools`.
- Перенесены исходники установленного сторожа браузера и шесть его проб. Личное состояние и секреты не переносились.
- Проверена официальная документация: штатная строка Codex выбирает готовые поля; дополнительная строка сделана через tmux.
- Пробы строки и привязки сессий написаны и запущены красными до реализации; реализованы сбор данных, изоляция и настройка полей.
- Проба установщика запущена красной до реализации; проверяет сохранение чужих хуков, оболочки и пользовательских полей при повторной установке.
- tmux 3.8 собран отдельно с существующей libevent; восстановлены прежние ссылки OpenSSL после попытки установки Homebrew.
- На проверке настоящего PTY найдено наследование CODEX_THREAD_ID родительского агента; отдельная проба воспроизвела дефект, состояние нового запуска сброшено до получения события своей сессии.
- Дополнительная строка выровнена слева: в узком терминале видны среда и адрес, обрезается конец. Запуск CLI ждёт настройки строки, чтобы быстрые команды не закрывали сервер tmux раньше установки параметров.
- Итог: 17 зелёных проб, повторная установка сохраняет настройки; проверка в настоящем PTY подтвердила строку среды и неизвестную сессию без ложной живости слушателя. Локальный репозиторий создан, origin ожидается от Дмитрия.

## 2026-10-15

- По явному разрешению Дмитрия применён диагностический патч из work-worker.
- Red: failure run удалялся; native terminal error snapshot отсутствовал. Green:
  ненулевой exit сохраняет run и печатает путь, launch снимает только собственную
  pane при failure в private0600 terminal-error.log; успех очищается как прежде.
- Все19 tests прошли на штатном /usr/bin/python3 (3.9). Исходная проблема
  самопроизвольного resume exit пока не воспроизвелась: тот же argv жил20s
  в отдельном диагностическом PTY, поэтому это исправление наблюдаемости.
- Установлены только два изменённых скрипта с private backup. Live invalid-argument
  проба установленной обвязки: wrapper/CLI exit2, run сохранён, лог600 содержит
  настоящую argument error, путь напечатан. Общие hooks, shell и LaunchAgent
  не менялись, другие сессии не завершались; запуск не создавал разговоров.

- Дополнительно по запросу пользователя: red два terminal-message tests, green
  report_failure показывает причину, объясняет неполный UUID и предлагает picker
  cx resume. Отображается только error/fatal строка (до500 символов), terminal
  escapes удаляются, предшествующая переписка из snapshot в вывод не переносится.
  Все21 tests green, обновлён один установленный скрипт с backup; реальный
  сохранённый failure теперь объясняет35 против36 символов без запуска модели.

- По прямому разрешению пользователя из work-worker реализован opted-in empty cx
  startup. Red actual run metadata args=[], green фиксированный positional service
  prompt; prompts/resume/unknown args нетронуты, query bounded/fail-closed. Отдельный
  red filesystem resolve error → green fallback обычного запуска.
- Live обнаружил подмену WORK_WORKER_HOME tmux shell: первый service turn успешно
  WW_SESSION_READY, но private QA runtime не выбран. Новые red explicit var/absence
  regressions → green terminal environment restore в launch.py. Первая собственная
  QA registration осталась закрытой в unconnected scope основного runtime без mail;
  не объявляется успешной isolation. Повторная native QA01a13fca… зарегистрирована
  в isolated runtime/open автоматически без manual prompt, затем active controller
  получил WW_CX_AUTOSTART_MAIL_OK; accepted/read/done с обеих сторон. Unknown controller
  negative probe корректно failed, outcome не переписывали. Никаких dev tasks peer.
- Полный28 tests на /usr/bin/python3 (3.9) green. Установлены main+launch только,
  private backups и baseline checks; hooks/trust/shell/LaunchAgent не менялись.
  QA native /quit, exact own broker остановлен после disconnect, temp scripts удалены.
  Новый пустой cx в connected/enabled проекте подхватывает feature без MCP reload.
