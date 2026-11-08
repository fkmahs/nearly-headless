@echo off
rem The kit's first command name, kept for chats and repos that still call it: it runs nh.cmd beside it.
rem The command and "exit /b" share one line: Windows reads a .cmd file as it runs.
call "%~dp0nh.cmd" %* & call exit /b %%ERRORLEVEL%%
