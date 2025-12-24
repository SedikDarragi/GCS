#!/usr/bin/env python3
"""Strip Python `#` comments from .py files using the tokenize module.
Creates a `.bak` backup for each file before overwriting.
Preserves shebang and encoding comments and leaves docstrings intact.
Usage: python3 scripts/remove_py_comments.py [path]
"""
import sys 
import os 
import io 
import tokenize 


def strip_comments (src :str )->str :
    out_tokens =[]
    try :
        gen =tokenize .generate_tokens (io .StringIO (src ).readline )
    except Exception :
        return src 

    for toknum ,tokval ,start ,end ,line in gen :
        if toknum ==tokenize .COMMENT :
        # preserve shebang (first line) and encoding declarations
            if start [0 ]==1 and tokval .startswith ("#!"):
                out_tokens .append ((toknum ,tokval ))
            elif "coding"in tokval .lower ():
                out_tokens .append ((toknum ,tokval ))
            else :

                continue 
        else :
            out_tokens .append ((toknum ,tokval ))
    try :
        return tokenize .untokenize (out_tokens )
    except Exception :
        return src 


def process_file (path :str ,backup =True )->bool :
    try :
        with open (path ,"r",encoding ="utf-8")as f :
            src =f .read ()
    except Exception as e :
        print (f"[SKIP] Cannot read {path }: {e }")
        return False 

    new =strip_comments (src )
    if new ==src :
        print (f"[UNCHANGED] {path }")
        return False 

    if backup :
        bak =path +".bak"
        try :
            with open (bak ,"w",encoding ="utf-8")as bf :
                bf .write (src )
        except Exception as e :
            print (f"[WARN] Could not write backup for {path }: {e }")

    try :
        with open (path ,"w",encoding ="utf-8")as f :
            f .write (new )
        print (f"[UPDATED] {path }")
        return True 
    except Exception as e :
        print (f"[ERROR] Could not write {path }: {e }")
        return False 


def walk_and_process (root :str ):
    processed =0 
    for dirpath ,dirnames ,filenames in os .walk (root ):

        if any (p in dirpath for p in ("/venv/","/.venv/","/.git/","/__pycache__")):
            continue 
        for fn in filenames :
            if not fn .endswith (".py"):
                continue 
            path =os .path .join (dirpath ,fn )

            if os .path .normpath (path )==os .path .normpath (__file__ ):
                continue 
            if process_file (path ,backup =True ):
                processed +=1 
    print (f"Done. Files updated: {processed }")


if __name__ =="__main__":
    root =sys .argv [1 ]if len (sys .argv )>1 else "."
    walk_and_process (root )
