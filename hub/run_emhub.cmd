@echo off
set EMHUB_DSN=postgres://monitor:monitor@localhost:5432/emhub
set EMHUB_HTTP=:8062
"C:\Git\equipment-monitor\hub\emhub.exe" -config "C:\Git\equipment-monitor\config.yaml"
