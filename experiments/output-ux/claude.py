import os,sys
from pathlib import Path
prompt=Path(sys.argv[1]).read_text()
os.execvp('claude',['claude','-p','--model','opus','--verbose','--output-format','stream-json','--permission-mode','dontAsk','--allowedTools','Bash,Read,Glob,Grep,Edit,Write','--',prompt])
