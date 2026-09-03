# Google Sign-In — GCP Setup Guide (NM Meet)

Backend already complete hai. `app/core/google_oauth.py` + `app/api/auth.py` mein
poora OAuth 2.0 / OpenID Connect flow (PKCE + state + id_token signature verify)
likha ja chuka hai. Ab sirf **Google Cloud Console pe ek OAuth client banana hai**
aur uski 2 values (`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`) app ko deni hain.

Ye doc do cheezein cover karta hai:

1. **Part A** — GCP pe kya-kya karna hai (step by step)
2. **Part B** — App side pe kya set karna hai (env vars + jo frontend ka kaam bacha hai)

---

## Ek line mein summary

| Chahiye kya | Kahan se milega | Kitna time |
|---|---|---|
| GCP project | console.cloud.google.com | 2 min |
| OAuth consent screen (Internal) | Google Auth Platform → Branding | 5 min |
| OAuth Client ID (type: **Web application**) | Google Auth Platform → Clients | 3 min |
| Client ID + Client Secret | Client banane ke baad popup mein | — |
| Redirect URI register karna | Usi client ke andar | 1 min |

**Koi API enable karne ki zaroorat nahi.** Hum sirf `openid`, `email`, `profile`
scopes maang rahe hain — ye Google Identity ke built-in scopes hain, inke liye
People API ya Admin SDK enable nahi karna padta. Naam aur photo dono `id_token`
ke andar hi aa jaate hain.

**App verification bhi nahi chahiye** — agar consent screen **Internal** rakha
(jo shreenm.com Google Workspace ke liye sahi option hai). Neeche step 3 dekho.

---

# PART A — Google Cloud Console pe kya karna hai

> Login karo `console.cloud.google.com` pe **shreenm.com wale Google Workspace
> account se**. Personal gmail se karoge to "Internal" option milega hi nahi.

---

## Step 1 — Project banao (ya existing choose karo)

1. `https://console.cloud.google.com` kholo.
2. Top bar mein project dropdown → **New Project**.
3. Fill karo:
   - **Project name:** `NM Meet`
   - **Organization:** `shreenm.com` ← ye zaroori hai, isi se Internal option unlock hota hai
   - **Location:** `shreenm.com` (organization)
4. **Create** dabao.
5. Ban jaane ke baad dropdown se `NM Meet` select kar lo (top bar pe naam dikhna chahiye).

> **Note:** Agar organization dropdown mein `shreenm.com` nahi dikh raha, matlab
> aap jis account se login ho wo Workspace ka member nahi hai, ya Workspace admin
> ne project banane ki permission nahi di. Admin se `Project Creator` role maango.

---

## Step 2 — Google Auth Platform kholo

Left menu → **APIs & Services** → **OAuth consent screen**
(2025 ke baad ye UI **"Google Auth Platform"** naam se aata hai — dono ek hi cheez hai).

Pehli baar khologe to **Get started** button milega — dabao.

---

## Step 3 — App information + Audience (SABSE IMPORTANT STEP)

Form 4 chhote screens mein aayega:

### 3.1 App Information
- **App name:** `NM Meet`
- **User support email:** `tech@shreenm.com` (ya jo bhi support mailbox ho)

### 3.2 Audience — **Internal chuno**

| Option | Kya hota hai | Hamare liye |
|---|---|---|
| **Internal** ✅ | Sirf shreenm.com ke Workspace users sign in kar sakte hain. Koi Google verification nahi. Koi 100-user test limit nahi. Consent screen pe "unverified app" warning nahi. | **YEHI CHUNO** |
| External ❌ | Duniya ka koi bhi Google account try kar sakta hai. Testing mode mein 100 users ki limit. Publish karne pe Google verification / brand review lag sakta hai. | Sirf tab jab Workspace hi na ho |

> Hamara app waise bhi code mein domain enforce karta hai
> (`ALLOWED_EMAIL_DOMAIN=shreenm.com`, `google_oauth.py:verify_id_token`), to
> personal gmail wapas laut jayega. Lekin **Internal** chunne se wo galti Google
> ke level pe hi ruk jaati hai — do layers, dono chahiye.

### 3.3 Contact Information
- **Email addresses:** `tech@shreenm.com`

### 3.4 Finish
- Google API Services User Data Policy pe checkbox tick karo → **Create**.

---

## Step 4 — Branding (optional but 5 min ka kaam)

Left menu → **Branding**

Yahan wo screen set hoti hai jo employee ko sign-in ke waqt dikhegi:

- **App logo:** `static/logo.png` upload kar do (square, 120x120 px minimum, <1MB)
- **Application home page:** `https://<your-service>.onrender.com`
- **Privacy policy / Terms of service:** Internal app ke liye optional hai, khaali chhod sakte ho
- **Authorized domains:** Internal app mein ye field aksar disabled/optional rehta hai.
  Agar enabled ho to `onrender.com` add kar do (custom domain lene par usko).

---

## Step 5 — Data Access / Scopes

Left menu → **Data Access** → **Add or remove scopes**

Sirf ye **teen** select karo:

| Scope | Kyun chahiye |
|---|---|
| `openid` | id_token milta hai — poori identity isi se verify hoti hai |
| `.../auth/userinfo.email` | Email address — user match aur domain check ke liye |
| `.../auth/userinfo.profile` | Naam aur profile photo — directory entry banane ke liye |

Teeno **non-sensitive** scopes hain. Calendar, Drive, Contacts — kuch bhi mat
add karna. Agar galti se sensitive scope add ho gaya to Google verification
maang lega aur setup atak jayega.

**Update** → **Save**.

---

## Step 6 — OAuth Client banao (yahan se Client ID/Secret milega)

Left menu → **Clients** → **+ Create client**

1. **Application type:** `Web application` ← dhyaan se, `Desktop` ya `Android` nahi
2. **Name:** `NM Meet Web` (ye sirf console mein dikhta hai, user ko nahi)

3. **Authorized JavaScript origins** — `+ Add URI` dabake add karo:

   ```
   http://localhost:8000
   https://<your-service>.onrender.com
   ```

   > Yahan **sirf origin** — path nahi, trailing slash nahi.
   > Actually hamare flow (server-side redirect) mein ye field zaroori nahi hai,
   > par future mein Google One Tap / JS button lagana ho to kaam aayega. Add
   > kar dena safe hai.

4. **Authorized redirect URIs** — ye **CRITICAL** hai. `+ Add URI`:

   ```
   http://localhost:8000/api/auth/google/callback
   https://<your-service>.onrender.com/api/auth/google/callback
   ```

   Ye bilkul **character-by-character** wahi hona chahiye jo `GOOGLE_REDIRECT_URI`
   env var mein hai. Zara sa farq — `http` vs `https`, `www` hai ya nahi, aakhir
   mein `/` laga hai ya nahi — to Google seedha
   **`Error 400: redirect_uri_mismatch`** de dega.

5. **Create** dabao.

6. Popup khulega jismein:
   - **Client ID** → `1234567890-abcdefg.apps.googleusercontent.com` jaisa dikhega
   - **Client Secret** → `GOCSPX-xxxxxxxxxxxxxxxx` jaisa dikhega

   **Dono copy karke safe jagah rakho.** Secret dobara dikh sakta hai console
   mein, par usko kabhi git mein, Slack pe, ya WhatsApp pe mat bhejo. Agar leak
   ho gaya to usi screen se **Reset secret** karke naya banao.

   > JSON download ka option bhi milega — download kar sakte ho, par us file ko
   > repo ke andar mat rakhna. `.gitignore` mein `*.json` nahi hai.

---

## Step 7 — Bas. GCP ka kaam khatam.

Aur kuch enable nahi karna:

- ❌ Billing account — Google Sign-In free hai
- ❌ People API / Admin SDK — id_token mein sab kuch already hai
- ❌ App verification — Internal app hai
- ❌ Service account — hum user ki taraf se sign-in kar rahe hain, server-to-server nahi
- ❌ API key — OAuth client alag cheez hai, API key ki zaroorat nahi

---

# PART B — App side pe kya set karna hai

## Step 8 — Local (laptop) pe test karna

`nm_meet/.env` mein (agar `.env` nahi hai to `.env.example` copy kar lo):

```env
GOOGLE_CLIENT_ID=1234567890-abcdefg.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxx
GOOGLE_REDIRECT_URI=http://localhost:8000/api/auth/google/callback
ALLOWED_EMAIL_DOMAIN=shreenm.com
GOOGLE_AUTO_CREATE_USERS=true
```

Server restart karo, phir check karo:

```bash
curl http://localhost:8000/api/auth/google/status
```

Milna chahiye:

```json
{"configured": true, "domain": "shreenm.com", "start_url": "/api/auth/google/start"}
```

`configured: false` aa raha hai → matlab env load hi nahi hua (`.env` galat folder
mein hai, ya server restart nahi kiya).

Ab browser mein kholo: `http://localhost:8000/login.html`
→ "Sign in with Google" button dikhna chahiye → dabao
→ Google ka account chooser → shreenm.com account se sign in
→ wapas `/` pe redirect, aur browser mein session cookie set ho jayegi.

Do galat cases bhi test kar lo:

- **Personal gmail se sign in karo** → wapas `/login.html` pe aana chahiye, upar
  red box mein: *"...is not a shreenm.com address."*
- **`GOOGLE_CLIENT_ID` khaali karke** page reload karo → button ki jagah
  "Not set up yet" wala notice aana chahiye, 503 dene wala dead button nahi.

> `localhost` ke liye Google `http` allow karta hai (special exception). Baaki
> kisi bhi host ke liye **https zaroori hai**.

---

## Step 9 — Render (production) pe set karna

Render dashboard → service `nm-meet` → **Environment**:

| Key | Value |
|---|---|
| `GOOGLE_CLIENT_ID` | console se copy kiya hua |
| `GOOGLE_CLIENT_SECRET` | console se copy kiya hua |
| `GOOGLE_REDIRECT_URI` | `https://<your-service>.onrender.com/api/auth/google/callback` |
| `ALLOWED_EMAIL_DOMAIN` | `shreenm.com` (already `render.yaml` mein set hai) |

Teeno keys `render.yaml` mein `sync: false` ke saath already declared hain —
matlab inki value dashboard mein bharni hai, git mein nahi jaayegi. Sahi hai.

Save karoge to Render khud redeploy kar dega.

> **Domain change karo to yaad rakhna:** custom domain (jaise
> `meet.shreenm.com`) lagate hi (a) `GOOGLE_REDIRECT_URI` update karo, aur
> (b) Google console ke Authorized redirect URIs mein naya URL **add** karo.
> Purana bhi rakh sakte ho jab tak migration chal raha hai.

---

## Step 10 — Sign-in ko compulsory kab banana hai

Abhi `SIGN_IN_REQUIRED=false` hai — koi bhi bina login kiye booking kar sakta
hai aur kisi ki bhi booking cancel kar sakta hai.

Google sign-in test ho jaane ke baad:

```env
SIGN_IN_REQUIRED=true
```

(Render pe `render.yaml` mein `"false"` → `"true"`, ya dashboard se override.)

Isse session, CSRF check, aur "sirf booking karne wala hi cancel kar sakta hai"
rule — teeno wapas on ho jayenge. Backend mein aur koi change nahi karna.

---

## Step 11 — Frontend (`static/login.html`) ✅ ban chuka hai

Pehle ye file thi hi nahi, jo ek asli bug tha: `app/api/auth.py:google_callback`
har failure par user ko yahan bhejta hai —

```python
target = f"/login.html?error={quote(detail)}"
```

— aur `static/` mein sirf `index.html` tha, to har Google error **404 page** ban
jaata tha aur asli reason gayab ho jaata tha. Ab `static/login.html` maujood hai.
Wo char cheezein karta hai:

1. **Button sirf tab dikhata hai jab server kehta hai Google configured hai.**
   Page load pe `GET /api/auth/google/status` call hota hai. `configured: false`
   par button ki jagah "Not set up yet" wala notice aata hai — warna employee
   ek aisa button dabata jo sirf 503 de sakta hai.
2. **`?error=` padhke plain English mein dikhata hai.** Message `messages.py` se
   already user-facing aata hai, isliye jaisa hai waisa hi `textContent` se
   dikhaya jaata hai (`innerHTML` se nahi — query string se aayi string kabhi
   HTML ki tarah execute nahi honi chahiye). Dikhane ke baad
   `history.replaceState` se query string hata di jaati hai, taki refresh karne
   par purana error dobara na dikhe.
3. **Pehle se signed-in user ko seedha grid pe bhej deta hai** — `GET /api/me`
   200 de to `/` pe redirect.
4. **Google ke [branding guidelines](https://developers.google.com/identity/branding-guidelines)
   follow karta hai** — white button, `#747775` border, 44px height, official
   four-colour "G" mark bina kisi recolour ke.

Button khud sirf ek link hai, koi fetch nahi:
`<a href="/api/auth/google/start">` — kyunki poora flow browser redirect hai aur
server ko state cookie us navigation par set karni hoti hai.

> **Design:** `index.html` ke hi CSS tokens use kiye hain (`--brand:#F5A300`,
> wahi dark header, wahi radii) taki dono page ek hi app lagein.

### Kya abhi bhi baaki hai (optional)

`index.html` mein "Sign out" button aur signed-in user ka naam header mein
dikhana — ye tabhi matlab rakhta hai jab `SIGN_IN_REQUIRED=true` ho jaye
(Step 10). Abhi `ME` variable khaali hai aur koi session nahi hoti.

---

# Troubleshooting — common errors

| Error | Matlab | Fix |
|---|---|---|
| `Error 400: redirect_uri_mismatch` | `GOOGLE_REDIRECT_URI` aur console ka registered URI exactly match nahi kar rahe | Dono ko side-by-side rakhke compare karo. http/https, trailing slash, port — sab check |
| `Error 401: invalid_client` | Client secret galat ya extra space ke saath paste ho gaya | Render env var mein value dobara paste karo, quotes/space hata ke |
| `Error 403: access_denied` / "app blocked" | Internal app hai aur user Workspace member nahi | Workspace account se sign in karo |
| Page pe *"NM Meet is only for shreenm.com accounts"* | Personal gmail se try kiya | Ye expected hai — `google_oauth.py:verify_id_token` ne roka. Work account use karo |
| *"Your sign-in could not be completed. Please try again."* | State/PKCE cookie expire ho gayi (10 min limit) ya cookie block hui | Fresh sign-in try karo. Browser third-party cookie block kar raha ho to bhi ho sakta hai |
| `configured: false` status pe | Env vars load nahi hue | Render pe save+redeploy hua? Local mein `.env` sahi folder mein hai? Server restart kiya? |
| Callback ke baad login hi nahi hua lagta | Cookie `Secure` flag ke saath set hui par site `http` pe hai | Production mein hamesha `https` use karo (`ENVIRONMENT=production` par cookie `secure=True` hoti hai) |
| Sign-in ke baad turant logout | `SECRET_KEY` rotate ho gaya | Render pe `SECRET_KEY` fix rehna chahiye — usko dobara generate mat karo |

---

# Security notes (jo pehle se handle hai)

Ye sab already code mein hai, sirf jaankari ke liye:

- **PKCE (S256)** — intercepted authorization code bekaar hai verifier ke bina
- **State parameter** — login-CSRF rok deta hai; signed cookie mein store hota hai, 10 min TTL
- **id_token signature verification** — Google ke public keys se cryptographically
  verify hota hai. "HTTPS se aaya hai isliye sahi hai" wala shortcut nahi liya gaya
- **`email_verified` check** — unverified email reject
- **Domain enforcement** — `@shreenm.com` ke alawa sab reject, naam leke
- **`hd` parameter** — Google ke account chooser ko domain tak limit karta hai (hint,
  asli enforcement upar wala hai)
- **Client secret kabhi git mein nahi** — `sync: false` (Render) + `.env` gitignored

---

# Checklist (print karke tick karo)

```
GCP:  (Amardeep ne kar diya — project shree-nm-meet)
  [x] Project banaya, shreenm.com organization ke andar
  [x] OAuth consent screen: Audience = Internal
  [x] Scopes: openid + userinfo.email + userinfo.profile (bas ye 3)
  [x] OAuth Client banaya, type = Web application
  [x] Redirect URI (localhost) add kiya
  [ ] Redirect URI (production) add kiya          <- BAAKI HAI
  [x] Client ID + Secret copy karke safe rakha

Local:
  [x] .env mein Google values daali
  [x] PORT 8001 → 8000 (registered redirect URI se match karne ke liye)
  [x] /api/auth/google/status → configured: true
  [x] /api/auth/google/start sahi Google URL banata hai, aur Google usko
      accept karta hai (koi redirect_uri_mismatch / invalid_client nahi)
  [ ] Browser se asli @shreenm.com account se sign in   <- BAAKI HAI (manual)
  [ ] Personal gmail se try kiya → reject hua          <- BAAKI HAI (manual)

Render:
  [ ] GOOGLE_CLIENT_ID set
  [ ] GOOGLE_CLIENT_SECRET set
  [ ] GOOGLE_REDIRECT_URI set (production URL ke saath)
  [ ] Redeploy hua, status endpoint check kiya
  [ ] Production pe ek real employee account se sign-in test kiya

Frontend:
  [x] static/login.html ban gaya
  [x] "Sign in with Google" button, /api/auth/google/start pe link
  [x] ?error= param handle hota hai
  [x] Button sirf tab dikhta hai jab status configured: true ho
  [x] index.html mein sign-out + signed-in user ka naam header mein
  [x] index.html: koi bhi API 401 de to seedha /login.html

Switch on:
  [ ] SIGN_IN_REQUIRED=true (browser wala test ho jaane ke baad)
```

---

# Kahan tak pahunche (3 Sep 2026)

**Ho gaya:**

- `.env` mein client id, secret, redirect URI aur domain set. `.env` gitignored
  hai; `.gitignore` mein `client_secret*.json` aur `*credentials*.json` bhi add
  kar diye, taki console se download ki hui JSON galti se commit na ho jaye.
- Local `PORT` 8001 se **8000**. Console mein redirect URI
  `http://localhost:8000/api/auth/google/callback` register hua hai aur Google
  usko character-by-character match karta hai — 8001 par sirf
  `redirect_uri_mismatch` milta.
- Verify kiya: `/api/auth/google/status` → `configured: true`, aur
  `/api/auth/google/start` ka banaya hua URL Google khud accept karta hai
  (sign-in page milta hai, error page nahi). Matlab client id, secret aur
  redirect URI teeno console se match kar rahe hain.
- `static/index.html` header mein signed-in user ka naam + **Sign out**. Session
  na ho to header pehle jaisa hi rehta hai, to `SIGN_IN_REQUIRED=false` par bhi
  kuch nahi tootta. Wizard ka default host ab signed-in employee hota hai.
- `index.html` ka `api()` ab 401 par `/login.html` bhej deta hai — yahi wo
  cheez hai jo `SIGN_IN_REQUIRED=true` ko asal mein kaam karne layak banati hai.

**Jo sirf haath se ho sakta hai (browser chahiye):**

1. `http://localhost:8000/login.html` — asli `@shreenm.com` account se sign in.
2. Personal gmail se try karke confirm karo ki red box wala reject aata hai.
3. Production: pehle Google console mein
   `https://<service>.onrender.com/api/auth/google/callback` **add** karo, phir
   Render ke env vars set karo.
4. Sab theek chale to `SIGN_IN_REQUIRED=true`.

---

**Reference files:**
`app/core/google_oauth.py` · `app/api/auth.py` · `app/config.py` · `.env.example` · `render.yaml`
