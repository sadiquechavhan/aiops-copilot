@echo off
cd /d "c:\Users\sadiq\Downloads\New_Project"
python -u tools/verify_rag.py > final_results.txt 2>&1
type final_results.txt