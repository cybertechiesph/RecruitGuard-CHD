# Security Rules

## 1. Purpose
This document summarizes the key cybersecurity rules and protection logic of RecruitGuard-CHD. It is intended to keep implementation aligned with the thesis security design and to guide consistent enforcement across modules.

---

## 2. Core Security Principles
- Secure-by-design
- Confidentiality
- Integrity
- Availability within prototype scope
- Accountability
- Non-repudiation
- Least privilege
- Defense-in-depth
- Trust minimization
- Evidence integrity
- Controlled disclosure

---

## 3. Authentication and Identity Verification Rules

### Internal Users
- Internal users must authenticate before accessing protected system pages.
- Only active internal accounts may authenticate successfully.
- Passwords must be stored using secure hashing.
- CAPTCHA must be required on internal login and internal password reset when
  CAPTCHA is enabled. Internal MFA verification and resend use attempt limits,
  cooldowns, rate limiting, and the authenticated first-factor session instead.
- For public deployment, CAPTCHA should use Google reCAPTCHA v2 with
  server-side token verification. The local arithmetic CAPTCHA is only a
  fallback for offline development and demonstration.
- reCAPTCHA verification requests must use Google's fixed HTTPS verification
  endpoint. Production credentials must be supplied through environment
  variables; Google's public test keys are allowed only in debug mode.
- Session handling must reduce reuse or misuse of authenticated access.
- Authenticated sessions must have a defined inactivity timeout. The default
  timeout is 30 minutes and is refreshed only by continued user activity.

### Applicants
- Applicant submission is accountless.
- OTP verification is required before final submission is accepted.
- OTP records must be stored in hashed form.
- OTP must expire after the defined validity window.
- Repeated invalid OTP attempts must be limited and require a new code after the configured attempt limit.
- Final submission must be blocked if OTP is invalid, expired, or incomplete.
- CAPTCHA must be required on applicant intake and applicant status lookup when
  CAPTCHA is enabled. Applicant OTP verification, resend, and final submission
  rely on the unguessable application token, OTP attempt limits, resend
  cooldowns, rate limiting, and verified-email state instead.
- Applicant-facing CAPTCHA tokens must be verified by the server before an
  intake or status lookup action is processed.

---

## 4. Access Control Rules

### Role-Based Access Control
- Access must be enforced server-side.
- Users may access only the functions, pages, and records appropriate to their role.
- The locked roles are:
  - Secretariat
  - HRM Chief
  - HRMPSB Member
  - Appointing Authority
  - System Administrator

### System Administrator Restriction
- System Administrator must not have default access to recruitment case content unless explicitly permitted by system rules.

### Stage-Bound Access
- Access may be limited further by the current workflow stage.
- Stage-restricted actions must not be exposed or permitted prematurely.

---

## 5. Routing Security Rules

### Level-Aware Routing
- Level 1 cases must be assigned to the Secretariat.
- Level 2 cases must be assigned to the HRM Chief.
- Secretariat must be blocked from processing Level 2 cases.

### COS Routing Rule
- The same Level 1 / Level 2 routing logic may be applied to COS as an internal office control.

### Override Rule
- Routing override is not automatic.
- If implemented, it must:
  - be authorized
  - be controlled
  - be audit-logged

---

## 6. Workflow Integrity Rules

### Stage Progression
- Stage progression must be enforced by the system.
- Stage skipping is not allowed.
- Stage advancement must require defined prerequisites.

### Stage Locking
- Finalized outputs may be stage-locked where applicable.
- Stage-locked records must not be editable through normal actions.
- Reopening must be controlled and audit-logged.

### Branch-Aware Rules
- Plantilla uses the stricter policy-aware path.
- COS uses a lighter flexible path.
- COS must not be forced into a rigid Plantilla-identical path.

---

## 7. Confidentiality Rules

### In Transit
- Data in transit must be protected using HTTPS/TLS in deployment.

### At Rest
- Selected sensitive stored data may be protected using AES-256-GCM or equivalent authenticated encryption.
- Encryption is for confidentiality and is separate from integrity hashing.

### Visibility Control
- Sensitive records must be visible only to authorized roles and only under appropriate workflow conditions.
- Secretariat must not have default visibility to finalized or closed Level 2 cases unless an explicit authorized basis exists.

---

## 8. Evidence Integrity Rules

### Recruitment Artifacts
- Recruitment files and generated artifacts must be stored in the Evidence Vault.
- Each artifact must preserve metadata such as case, stage, uploader, and timestamp.

### Hashing
- SHA-256 hash values must be generated and stored for evidence-related files where applicable.

### Version Preservation
- Evidence versions must be preserved without silent overwrite.
- Replacements or updates must produce a traceable version record.

---

## 9. Audit Logging Rules

### Required Audit Elements
Audit events should preserve:
- actor identity
- actor role
- action performed
- timestamp
- case reference
- workflow stage
- relevant details

### Critical Logged Events
The following categories must be logged where applicable:
- account and role changes
- recruitment entry creation/update/status changes
- applicant submission finalization
- routing actions and overrides
- stage finalization
- reopen actions
- screening finalization
- exam-related actions
- rating submission
- deliberation finalization
- CAR generation/finalization
- final decision recording
- notification events where traceability is needed
- export generation
- sensitive access events
- denied evidence download and export attempts where applicable
- audit-log viewing where applicable

---

## 10. Export and Controlled Disclosure Rules

### Export Control
- Export must be allowed only to authorized roles.
- Export requests must be audit-logged.

### Export Bundle Requirements
Export bundles should include, where applicable:
- required recruitment records and outputs
- evidence inventory
- integrity verification output

### Disclosure Handling
- Export is treated as a controlled disclosure surface.
- Unauthorized export must be blocked.

---

## 11. Rate Limiting and Logging Rules
- Public and internal request surfaces must have basic application-level rate
  limiting in addition to any deployment-layer throttling.
- Rate-limit thresholds must be configured through environment variables.
- Logs must avoid storing passwords, OTPs, tokens, secrets, cookies,
  authorization headers, and CSRF values.
- Production logging should use redaction filters before records are written to
  the configured log sink.

---

## 12. Availability Rules Within Prototype Scope
- The system should remain stable during scheduled evaluation sessions.
- The academic prototype does not include destructive testing.
- Denial-of-service testing and brute-force campaigns are out of scope.
- Availability assessment is limited to controlled prototype conditions.

---

## 13. Scope Constraints
- Recruitment only
- Full onboarding is out of scope
- Offboarding, termination, payroll, and full employee lifecycle management are out of scope
- Use dummy or synthetic data only during development, testing, and evaluation

---

## 14. Developer Reminders
- Never hard-code secrets
- Use environment variables for:
  - Django secret key
  - DB credentials
  - email credentials
  - encryption keys
- Keep access enforcement server-side
- Keep audit logging consistent across modules
- Keep Plantilla and COS branch behavior intentionally distinguishable
