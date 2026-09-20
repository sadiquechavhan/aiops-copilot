@echo off
cd /d "c:\Users\sadiq\Downloads\New_Project"
py -3.12 tools/verify_rag.py > rag_output.txt 2>&1
type rag_output.txt