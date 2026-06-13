#!/usr/bin/env pwsh
$ErrorActionPreference = "Stop"

Write-Host "=== Class Sync - Deploy to GitHub ===" -ForegroundColor Cyan

git add .

Write-Host "`nFiles staged:" -ForegroundColor Yellow
git status --short

$msg = "feat: add Render deployment config and production fixes"
git commit -m $msg

Write-Host "`nPushing to GitHub..." -ForegroundColor Yellow
git push

Write-Host "`nDone! Now go to render.com to deploy." -ForegroundColor Green
