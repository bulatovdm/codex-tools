# Журнал

## 2026-10-23 — Исправления по аудиту ожиданий и остановок

- По поручению Дмитрия исправлены пробелы аудита: общий учёт активного вопроса
  подавляет второй Stop-пуш; краткий ID различает одноимённые сессии; approval
  context читается инкрементально и сохраняется после больших результатов инструментов.
- Доставка использует очередь, lease и отдельные подтверждения каналов. Сбои сети
  повторяются с backoff до снятия ожидания; ntfy получает постоянный sequence_id.
  Неоднозначный Telegram timeout и отсутствие восстановления очереди после reboot
  явно отмечены как ограничения, гарантия exactly-once не заявляется.
- Добавлены наблюдатель локального CLI по PID+времени запуска и сообщение кода
  завершения из cx. Штатные SessionEnd/exit0/Ctrl-C не дают аварийного пуша;
  grace и снятие failure_token закрывают найденную повторным аудитом гонку выхода.
- Продолжающий работать exec session_id сохраняется до видимого завершения.
  Тесты покрывают async→Stop, большой вывод, независимый retry каналов, реальные
  конкурирующие процессы отправки, PID reuse и гонку штатного выхода.
- Проверки: 67 passed, 5 history checks skipped на Python 3.9. Установлены
  notify-phone.py и launch.py с private backup, определения hooks и delivery config
  сохранены. Наблюдатель подтвердил исчезновение собственного временного процесса
  и ровно один сигнал с замоканной сетью; в текущей native-сессии owner зарегистрирован.
  Новый обработчик exit status применяется к новым запускам cx; текущие CLI
  подхватывают наблюдатель на ближайшем событии доверенного хука.

## 2026-10-23 — Повторы уведомлений разрешений

- По жалобе Дмитрия прочитана история общего ntfy-топика: частые пуши были
  Needs permission при продолжающейся работе сессий. В их turn_context включён
  auto_review; немедленный PermissionRequest ошибочно считался ожиданием человека.
- Добавлены исключение автоматической проверки/never, отложенная отправка
  незавершённого запроса человеку и отмена при выполнении/завершении/прерывании.
  Каждый запрос и вопрос дедуплицируется по turn/call ID либо хешу инструмента.
- Повторный UserPromptSubmit одного turn больше не сбрасывает уведомление;
  служебный Work Worker startup signal также не считается человеческим вводом.
- Проверки: 53 passed, 5 существующих history checks skipped на Python 3.9.
  Исправленный скрипт установлен атомарно с private backup; hooks/trust/config
  не менялись. На реальном turn_context текущей сессии 20 запросов auto_review
  не запускают ни одного процесса отправки; проба использует изолированное состояние.

## 2026-10-23

- По прямому поручению Дмитрия реализован аналог phone notifications Claude Code:
  отложенный Stop, отмена и дедупликация по человеческой реплике, разрешения и вопросы.
- Добавлены общий delivery config, независимая доставка ntfy/Telegram с timeout,
  исходный проект/worktree и доступное название сессии без текста модели.
- Установщик сохраняет чужие hooks/config и повторно использует конфиг Claude;
  при его отсутствии создаёт приватный случайный топик. Глобальная установка
  и настоящая отправка в этой работе не выполнялись.
- Добавлены локальные проверки с подставным curl, включая настоящий detached worker,
  отмену, synthetic prompts, source=exec/subagents, фоновые вызовы и worktrees.
- После отдельного разрешения Дмитрия выполнена установка с резервными копиями.
  Используется существующий ~/.claude/notify.env, новый топик не создавался.
  Все восемь notify-phone hooks проверены и доверены по отдельности через /hooks
  в CLI 0.162.1; меню подтвердило active без pending review. Кнопка trust all была
  отклонена auto-review, вместо неё применено адресное доверие каждому определению.
  Один настоящий тестовый ntfy-пуш принят сервером (curl exit 0); получение на
  телефоне со стороны устройства не проверялось. 45 tests passed, 5 history tests
  skipped на системном Python 3.9. Другие сессии не перезапускались.
- Дмитрий подтвердил получение тестового пуша и штатного отложенного уведомления
  после возобновления разговора: доставка до телефона проверена пользователем.

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


## 2026-10-23 · возврат в detached cx

- Native session работавшего агента:01a169c9-e25b-71f3-afe0-b42db869cce0; hook
  journal находится в work-worker. Цель — устранить повторный запуск при живом
  detached разговоре. На входе codex-tools checkout чист, HEAD33f584d.
- Пользователь разрешил реализацию, установку, commit/push и после успешного push
  завершение точного старого Codex в Work Worker; остальные процессы не трогаем.
- Red: exact/bare resume создавали новый tmux server вопреки существующему live
  run; assertion must reuse live run вместо старта второго Codex. Реализованы
  exact ID discovery, project menu, attached/duplicate checks и recheck before attach.
- Real tmux выявил внешние quotes в pane_start_command: отдельный test red→green
  закрепил нормализацию. QA на временном HOME с sleep stand-in сначала потребовала
  явного tmux path; это ограничение QA окружения, не дефект продукта.
- Green:83 Python tests,5 existing platform skips. Изолированная real tmux/PTY
  проверка подтвердила discovery, wrapper attach в existing pane без нового run,
  clean detach; только собственный QA server удалён. Это tmux integration evidence,
  не запуск новой model/native conversation. Нет новых mail/проектов.
- Установка ограничена scripts/codex-tools.py с private backup; full installer
  не используется, globals/hooks/trust/shell/LaunchAgent не меняются.
- Actual installation выполнена с private backup; byte equality source/installed
  подтверждена. Установленный launcher read-only обнаружил прежний detached native
  разговор и текущий attached разговор раздельно. Полный suite в исходном checkout:
  83 tests green,5 existing skips; git diff --check clean. Далее commit/push,
  после подтверждения origin — остановка разрешённого пользователем старого
  процесса с повторной проверкой PID/start time/argv/точного открытого rollout.

## 2026-10-25 · жизненный цикл cx и расширенный resume

- Повторный screenshot writer conflict; пользователь обычно использует Ctrl-C,
  запросил все сценарии. Readonly диагностика: один живой detached Work Worker
  run988d2e4bc018445c8ed683dc6fef2444, native01a13b63-241d-7723-a926-9aeed94857ac.
  Сам pane не показывает writer conflict; старый клиент реально продолжает жить.
  Старые процессы и locks не изменяли.
- Новая политика отдельных tmux серверов: close завершает запуск; Ctrl-B,D
  сохраняет, reattach снова включает завершение при close. Real tmux выявил
  ошибку экранирования ';' в bind-key, исправлено на nested escaped separator;
  шесть интеграционных сценариев green. Проверяются только собственные QA servers.
- Resume расширен: -C, project subdirectories, --all, --last с явным выбором,
  точные имена из ограниченного локального index после проверки live identity.
  Overrides/prompt не отбрасываются при attach; старый native fallback сохраняется.
  Добавлены проверки ambiguity, Ctrl-C/EOF, exit status/cleanup после reattach,
  исчезновения run во время выбора; corrupt/stale/foreign metadata исключаются.
- Реальный пустой Codex0.162.1: idle Ctrl-C exit0; отдельное закрытие PTY с source
  wrapper завершило native child и собственный tmux. Hooks disabled, model task
  не отправляли. Active interrupt моделируется stand-in, не выдается за model QA.
  Физическая перезагрузка не выполнялась; отсутствие живой pane покрыто тестами.
- CODEX_TOOLS_LIVE_TMUX=1 /usr/bin/python3 -m unittest discover -s tests:
  102 tests,5 existing platform skips. Только scripts/codex-tools.py установлен
  атомарно с baseline HEAD check; private backup lifecycle-20261025-170948.
  Source/installed bytes совпали. Hooks/trust, broker и глобальные настройки
  не менялись; старый server получит политику только после cx resume.
