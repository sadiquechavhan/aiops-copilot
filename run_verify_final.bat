@echo off
cd /d "c:\Users\sadiq\Downloads\New_Project"
python tools/verify_rag.py > verify_batch.txt 2>&1
type verify_batch.txt