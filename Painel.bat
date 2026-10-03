@echo off
rem Dois cliques aqui abrem o painel no navegador. Deixe esta janela aberta enquanto usa o painel.
rem Antes de abrir, instala o que falta e atualiza as dependencias: o Python aqui (pelo winget) e o
rem resto em dependencias.py (programas pelo winget, bibliotecas do requirements.txt pelo pip).
cd /d "%~dp0"
title Painel VSCO
setlocal
rem Pacote do winget instalado quando nao ha Python 3.8+ no computador.
set "PYTHON_WINGET=Python.Python.3.14"
set "WINGET_OPCOES=--exact --silent --disable-interactivity --accept-package-agreements --accept-source-agreements"
set "TESTE=import sys; sys.exit(sys.version_info < (3, 8))"

set "WINGET="
where winget >nul 2>&1 && set "WINGET=winget"

set "PY="
python -c "%TESTE%" >nul 2>&1 && set "PY=python"
if not defined PY (py -3 -c "%TESTE%" >nul 2>&1 && set "PY=py -3")

rem Ja instalado: atualiza dentro da mesma versao (ex.: 3.14.6 -> 3.14.7), pelo pacote Python.Python.3.x dela.
if defined PY if defined WINGET (
    for /f "tokens=1,2" %%a in ('%PY% -c "import sys; print(*sys.version_info[:2])"') do (
        echo [Python] conferindo o winget ^(Python.Python.%%a.%%b^)...
        winget upgrade %WINGET_OPCOES% --id Python.Python.%%a.%%b
    )
)

if not defined PY if defined WINGET (
    echo [Python] sem Python 3.8+ no computador: instalando %PYTHON_WINGET% pelo winget...
    winget install %WINGET_OPCOES% --scope user --installer-type burn --id %PYTHON_WINGET%
    rem o PATH desta janela nao muda com a instalacao: procura na pasta padrao do instalador do python.org
    for /d %%d in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if not defined PY ("%%d\python.exe" -c "%TESTE%" >nul 2>&1 && set PY="%%d\python.exe")
    )
)

if not defined PY (
    echo.
    echo Python 3.8+ ausente.
    if not defined WINGET echo Sem winget neste Windows para instalar sozinho.
    echo Instale pelo site https://www.python.org/downloads/ ^(marque "Add python.exe to PATH"^) e abra o Painel.bat de novo.
    pause
    exit /b 1
)

%PY% dependencias.py
if errorlevel 1 (
    pause
    exit /b 1
)
%PY% painel.py
if errorlevel 1 pause
