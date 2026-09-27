@echo off
rem Atalho de duplo clique para o instalar.ps1.
rem -ExecutionPolicy Bypass vale só para esta execução: o Windows bloqueia
rem scripts .ps1 por padrão, e assim a pessoa não precisa mudar configuração.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalar.ps1"
pause
