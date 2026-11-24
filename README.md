# SARATHI local portal

This is a local, full-stack demonstration portal. It stores accounts, marks, chat messages, and evaluation summaries in `data/sarathi.sqlite3` on the computer running the service. It is not connected to a public hosting provider.

## Start the portal on Windows

1. Install Python 3.10 or later if it is not already installed.
2. Double-click `start_sarathi.bat`.
3. Keep the server window open. The portal opens at `http://127.0.0.1:8765`.
4. Create a student account using a unique password of at least 12 characters.

To stop the service, close the SARATHI server window or press Ctrl+C in it.

## Create a mentor account

Mentor access cannot be granted by public sign-up. Run `create_mentor.bat` on the same computer and enter the mentor account details when prompted. Then sign in through the portal. Students can chat with provisioned mentors; mentors can chat with registered students. Each conversation is private to its two participants.

## Evaluate the model

Sign in with a mentor account and open **Model evaluation**. Upload a CSV or paste CSV text with these columns:

`attendance,assignments,study_hours,consistency,confidence,final_score`

The portal starts with a clearly labeled synthetic demo model so the predictor can be explored immediately. Its 120 generated rows and metrics are not evidence of real-world accuracy. To replace it, the file must contain 20–5,000 complete rows. SARATHI uses an 80/20 shuffled row holdout, fits a ridge linear regression model, and reports MAE, RMSE, R², and mean-baseline MAE. Uploaded rows are evaluated in memory and then discarded; the evaluation summary and fitted model coefficients are saved. The latest mentor-trained model is shared for predictions by all accounts in this local portal. This evaluator does not group repeated rows by student and is not suitable for research claims on longitudinal student records. Use properly governed, de-identified data for a meaningful evaluation.

## Security and deployment boundary

- Passwords are hashed with PBKDF2-HMAC-SHA256 and a unique random salt.
- Sessions use random opaque tokens stored as hashes in SQLite, HttpOnly and SameSite=Lax cookies, CSRF tokens, origin checks, a 12-hour expiry, and login/sign-up rate limits.
- Student marks are scoped to the signed-in user. Chat is one-to-one, and evaluation pages require a provisioned mentor account.
- The server binds to `127.0.0.1` by default and uses no third-party runtime packages.

This configuration is for a local demonstration. It does not include email verification, password reset, account recovery, multifactor authentication, encrypted database storage, backups, institutional role approval, moderation, or audit exports. Do not expose it to the internet or a shared network as-is. A real deployment needs institution-approved identity and roles, HTTPS with Secure cookies, protected database storage and backups, access logging, retention/deletion policy, privacy review, and a supported production server. Only then should actual student data be used.

## Database

The SQLite database is created automatically on first start in `data/`. Removing that file permanently removes local accounts and portal records. Back it up securely before any manual maintenance.

