@echo off
rem Dois cliques aqui abrem o painel no navegador. Deixe esta janela aberta enquanto usa o painel.
cd /d "%~dp0"
title Painel VSCO
python painel.py
if errorlevel 1 pause
