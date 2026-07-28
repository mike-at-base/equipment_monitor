@echo off
set EMHUB_DSN=postgres://monitor:monitor@localhost:5432/emhub
set EMHUB_HTTP=:8062
set EMHUB_UDP_PORT=15020
"C:\Git\equipment-monitor\hub\emhub.exe"
