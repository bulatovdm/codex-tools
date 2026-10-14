# tmux

Обычно достаточно `brew install tmux`. Установщик обвязки не меняет пакеты Homebrew автоматически.

На машине macOS 13.7.8 tmux 3.8 собран из архива, загруженного Homebrew, с существующей libevent 2.1.12. Дополнительные utf8proc и jemalloc отключены. Он установлен отдельно в `~/.local/share/codex-tools/tmux`; этот путь выбирается раньше системного tmux.

Воспроизводимая сборка после распаковки исходников tmux 3.8:

```sh
./configure --prefix="$HOME/.local/share/codex-tools/tmux" \
  --disable-utf8proc --disable-jemalloc \
  CFLAGS=-I/opt/homebrew/include LDFLAGS=-L/opt/homebrew/lib
make -j4
make install
```

Нужны Command Line Tools и libevent. Исходники, бинарник и чужая библиотека не включены в этот репозиторий. Лицензия tmux сохраняется рядом с установленным бинарником.

Попытка Homebrew при первой установке успела установить pkgconf 3.0.8 и OpenSSL 4.0.3. Дальнейшая сборка зависимостей остановлена; прежние ссылки OpenSSL 3.6.5 восстановлены. Новые пакеты не удалялись, чтобы не затронуть возможных потребителей.
