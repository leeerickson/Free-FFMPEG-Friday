# Code signing (Microsoft Trusted Signing)

Windows **Smart App Control** blocks unsigned apps outright, and SmartScreen warns about them. Signing fixes both. The build workflow already has a signing step. It stays switched off until the settings below exist, then signs every unsigned `.exe`, `.dll` and `.pyd` in the app folder (including the bundled FFmpeg and libsrt) and checks the result.

Microsoft has been renaming this service (Trusted Signing / Artifact Signing), so the portal wording may differ slightly from the steps below.

## 1. Azure: the signing account (about $10 a month, Basic tier)
1. Sign in at https://portal.azure.com with a Microsoft account and create a **pay-as-you-go subscription** if you don't have one.
2. **Subscriptions** → your subscription → **Resource providers** → register **Microsoft.CodeSigning**.
3. Create a **Trusted Signing account**: pick a resource group, a name (for example `srtstreamer`), a region and the **Basic** SKU. Note the account's **endpoint** (for example `https://eus.codesigning.azure.net/` for East US).
4. On that account, give yourself the **Trusted Signing Identity Verifier** role (Access control (IAM) → Add role assignment).
5. **Identity validation** → **New identity** → **Public** → **Individual** (or Organization, if you sign as a company). You'll verify with a government ID. Approval can take from a few hours to several days.
6. When validation succeeds: **Certificate profiles** → **Create** → **Public Trust**, linked to that identity. Note the **profile name**.

## 2. Azure: a sign-in for GitHub Actions
1. **Microsoft Entra ID** → **App registrations** → **New registration** (for example `srt-streamer-ci`). Note the **Application (client) ID** and **Directory (tenant) ID**.
2. In that app registration: **Certificates & secrets** → **New client secret**. Copy the secret's **value**.
3. Back on the Trusted Signing account: **Access control (IAM)** → **Add role assignment** → **Trusted Signing Certificate Profile Signer** → assign it to the app registration.

## 3. GitHub: tell the workflow
In the repository: **Settings** → **Secrets and variables** → **Actions**.

| Kind | Name | Value |
|---|---|---|
| Secret | `AZURE_TENANT_ID` | Directory (tenant) ID |
| Secret | `AZURE_CLIENT_ID` | Application (client) ID |
| Secret | `AZURE_CLIENT_SECRET` | the client secret's value |
| Variable | `TRUSTED_SIGNING_ENDPOINT` | the account endpoint, for example `https://eus.codesigning.azure.net/` |
| Variable | `TRUSTED_SIGNING_ACCOUNT` | the signing account name |
| Variable | `TRUSTED_SIGNING_PROFILE` | the certificate profile name |

The next build then signs in two stages: first the bundled `ffmpeg.exe` and `srt.dll` (the single-file exes unpack these when they start, and Windows checks them again then), then the finished `SRT Streamer.exe` files. It verifies every signature and self-tests the signed exes. Release zips are signed from then on. The signing commands live in `packaging/sign.ps1`.
