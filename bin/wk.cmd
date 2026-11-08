@echo off
rem The command and "exit /b" share one line: Windows reads a .cmd file as it runs, so an update that changes
rem this file mid-command must never make it run anything twice.
rem When this laptop's settings name another checkout as its kit (kit = "...", written by wk setup), forward to it,
rem so wk from this folder on the PATH runs that one.
setlocal
set "WK_S=%WORKKIT_SETTINGS%"
if not defined WK_S set "WK_S=%USERPROFILE%\.workkit\settings.toml"
for %%k in ("%WK_S%") do set "WK_S=%%~fk"
set "WK_T="
if not defined WORKKIT_FORWARDED if exist "%WK_S%" for /f "usebackq tokens=1,* delims==" %%a in (`findstr /r /b /c:"kit *=" "%WK_S%"`) do if not defined WK_T for %%c in (%%b) do if not defined WK_T set "WK_T=%%~c"
for %%k in ("%~dp0..") do set "WK_HERE=%%~fk"
if defined WK_T for %%k in ("%WK_T%") do set "WK_T=%%~fk"
if defined WK_T if exist "%WK_T%\bin\wk.cmd" if /i not "%WK_T%"=="%WK_HERE%" set "WORKKIT_FORWARDED=%WK_HERE%" & call "%WK_T%\bin\wk.cmd" %* & call exit /b %%ERRORLEVEL%%
set "PYTHONPATH=%~dp0..;%PYTHONPATH%"
set "PYTHONIOENCODING=utf-8"
py -P -m wk %* & call exit /b %%ERRORLEVEL%%
