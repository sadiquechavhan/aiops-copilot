#!/usr/bin/env powershell
Set-Location c:\Users\sadiq\Downloads\New_Project

Write-Host "=== Checking git status ==="
$status = git status 2>&1
$status | Out-File -Encoding UTF8 git_status.txt
Get-Content git_status.txt

Write-Host "`n=== Adding all changes ==="
git add -A 2>&1

Write-Host "`n=== Committing ==="
$commit = git commit -m "Session 9: Closed-loop automation - whitelisted remediation, approval gate, recovery verification, audit logging" 2>&1
$commit | Out-File -Encoding UTF8 git_commit.txt
Get-Content git_commit.txt

Write-Host "`n=== Pushing to origin ==="
$push = git push origin main 2>&1
$push | Out-File -Encoding UTF8 git_push.txt
Get-Content git_push.txt

Write-Host "`n=== Done ==="