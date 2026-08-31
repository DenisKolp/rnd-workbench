# Проверенный patch OmniVoice

Patch `desktop-realtime-and-cancel.patch` применяется к
`ServeurpersoCom/omnivoice.cpp` на ревизии
`4f33af825d66e6ef1cb185e87b4589cacf747291`.

Он содержит три связанные настройки пилотного голосового контура:

- параметры быстрых MaskGIT/chunked-запросов в `tts-server`;
- desktop-realtime post-processing без длинного нулевого стыка;
- кооперативную отмену при закрытии HTTP-соединения во время перебивания.

`macos/build-omnivoice.sh` проверяет ревизию и применяет patch идемпотентно.
Это делает нативную сборку воспроизводимой: локально изменённый `vendor/` не
нужно и нельзя публиковать целиком.
