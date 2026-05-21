# ASVS Security Testing Matrix

This matrix maps RecruitGuard-CHD security testing to selected applicable OWASP ASVS 5.0.0 requirements at specific requirement ID level. The target basis is ASVS Level 2 where applicable, with non-destructive testing only and dummy or synthetic data.

## Scope Statement

The security test case sheet should state:

> Security testing for RecruitGuard-CHD is aligned with selected applicable OWASP ASVS 5.0.0 Level 2 requirements. Each security test is mapped to STRIDE threat categories, measurable security metrics, and Chapter 4 evidence. Requirements that are outside the system scope, such as OAuth/OIDC, WebRTC, self-contained tokens, and destructive denial-of-service testing, are excluded or marked not applicable.

## Detailed Mapping

| Test ID | Security Testing Area | Security Research Objective | ASVS 5.0.0 Requirement ID | STRIDE Mapping | Security Test / Metric | Expected Evidence |
| --- | --- | --- | --- | --- | --- | --- |
| SEC-01 | Authentication pathway control | Verify that only valid internal users can complete internal authentication and that authentication events are traceable. | V6.1.1, V6.1.3, V6.3.1, V6.3.4, V16.3.1 | Spoofing, Repudiation | Invalid internal login denial rate; inactive account denial rate; authentication event logging completeness | Login test result, audit log, screenshot |
| SEC-02 | Password security | Verify that internal account passwords follow secure password handling controls. | V6.2.1-V6.2.7, V6.2.8, V6.2.10 | Spoofing | Weak/common password rejection; password change availability; current password required; password input masked; paste/password manager not blocked | Password form test, validation result, UI screenshot |
| SEC-03 | Password reset | Verify that forgotten-password recovery uses secure tokenized reset links and does not bypass MFA/account controls. | V6.4.3, V6.3.4, V16.3.1 | Spoofing, Repudiation | Password reset token works only through generated link; applicant accounts cannot receive internal reset; reset event logged | Reset email, audit log, test result |
| SEC-04 | Password reuse prevention | Verify that recently used internal passwords cannot be reused after password change or reset. | V6.2.2, V6.2.3, V6.4.3 | Spoofing | Recent password reuse rejection rate = 100% | Password history test, form error screenshot |
| SEC-05 | Password strength meter | Verify that password entry gives users strength feedback without replacing server-side validation. | V6.2.1, V6.2.4, V6.2.6 | Spoofing | Strength meter visible on password creation/change/reset forms; weak password still rejected server-side | Form screenshot, validation result |
| SEC-06 | Applicant email OTP | Verify that accountless applicant submission cannot be finalized without a valid OTP. | V6.5.1, V6.5.3, V6.5.4, V6.5.5, V6.6.2, V6.6.3 | Spoofing, Tampering | OTP bypass success rate = 0%; invalid OTP rejection = 100%; expired OTP rejection = 100%; reused OTP rejection = 100% | Applicant OTP email, submission result, audit/test log |
| SEC-07 | Internal email OTP MFA | Verify that internal login requires password plus email OTP before protected access. Email OTP is treated as out-of-band OTP, not TOTP. | V6.3.3, V6.5.1, V6.5.3, V6.5.4, V6.5.5, V6.6.2, V6.6.3, V16.3.1 | Spoofing, Repudiation | MFA bypass success rate = 0%; expired OTP rejection = 100%; wrong OTP lock/rate-limit works; MFA event logged | MFA email, challenge record, audit log, screenshot |
| SEC-08 | Login throttling and lockout alerting | Verify that repeated failed internal login attempts are controlled and recorded. | V6.1.1, V6.3.1, V16.3.1, V16.3.3 | Spoofing, Repudiation | Lockout applied at configured threshold; failed login counter resets after successful password step; alert generated when configured | Login attempt records, alert email, audit log |
| SEC-09 | Session protection | Verify that authenticated sessions are server-side controlled and invalidated when required. | V7.1.1, V7.2.1-V7.2.4, V7.3.1, V7.3.2, V7.4.1, V7.4.4, V7.5.1 | Spoofing, Information Disclosure | Anonymous redirect rate = 100%; protected page requires completed MFA; logout invalidates session; sensitive account change requires controlled process | Redirect result, session test, logout test |
| SEC-10 | RBAC and admin boundary | Verify that functions and records are restricted by role and that System Admin does not inherit recruitment case access by default. | V8.1.1, V8.2.1, V8.2.2, V8.3.1, V8.3.2 | Elevation of Privilege, Information Disclosure | Unauthorized page/action denial rate = 100%; System Admin case-content denial by default; non-admin cannot access user directory/audit routes | Role access matrix, 403/redirect screenshot, audit log |
| SEC-11 | Level-aware routing restriction | Verify that Level 1 and Level 2 cases follow defined routing and that Secretariat cannot process Level 2 without authorized override. | V2.2.1, V2.3.1, V2.3.2, V8.2.1, V8.2.2, V8.3.1, V16.3.2, V16.3.3 | Tampering, Elevation of Privilege, Information Disclosure | Level 1 routes to Secretariat; Level 2 routes to HRM Chief; Secretariat Level 2 block rate = 100%; override logged | Routing record, blocked action screenshot, audit log |
| SEC-12 | Stage-bound workflow enforcement | Verify that workflow stages cannot be skipped and finalized outputs cannot be edited through normal actions. | V2.3.1, V2.3.2, V2.3.3, V8.3.1, V16.2.1, V16.3.3 | Tampering, Repudiation | Stage-skip prevention = 100%; future-stage post blocked; finalized record edit rejection = 100%; reopen action logged | Workflow test sheet, locked-stage screenshot, audit log |
| SEC-13A | SQL injection resistance | Verify that database-backed inputs resist SQL injection and use ORM/parameterized query protections. | V1.2.4, V2.1.1, V2.2.1 | Tampering, Information Disclosure | SQL injection success rate = 0%; no unauthorized records returned; no database error or stack trace exposed | Burp/ZAP/manual result, request/response screenshot, server/test log |
| SEC-13B | General input validation | Verify that application inputs enforce expected structure, ranges, and business rules. | V1.2.1-V1.2.3, V1.3.3, V2.1.1, V2.2.1 | Tampering | Invalid business value rejection rate = 100%; invalid payload server-error rate = 0% | Validation screenshot, test result, request/response sample |
| SEC-14 | File upload control | Verify that uploaded applicant/evidence files are bounded, type-checked, and protected against unsafe file handling. | V5.1.1, V5.2.1, V5.2.2, V5.3.1, V5.3.2 | Tampering, Denial of Service, Information Disclosure | Oversized upload rejection = 100%; invalid file signature rejection = 100%; uploaded files not executable as server code | Upload test result, file validation screenshot |
| SEC-15 | File download control | Verify that file downloads use controlled filenames and authorized access. | V5.4.1, V5.4.2, V8.2.2, V14.2.1 | Information Disclosure, Tampering | Unauthorized file download denial rate = 100%; assigned handler can download only permitted evidence | Download response, access-denied result |
| SEC-16 | Evidence integrity and confidentiality | Verify that evidence files preserve integrity, metadata, and protected storage. | V5.3.2, V11.2.1, V11.2.2, V11.3.1, V11.4.1, V14.1.1, V14.2.1, V16.2.1 | Tampering, Repudiation, Information Disclosure | SHA-256 digest stored; hash verification success = 100%; stage/case/uploader metadata completeness = 100%; encrypted evidence storage verified | Evidence inventory, digest output, database/admin evidence record |
| SEC-17 | Version preservation | Verify that re-uploading or replacing evidence does not silently overwrite prior versions. | V2.3.2, V5.3.2, V16.2.1, V16.3.3 | Tampering, Repudiation | Silent overwrite prevention = 100%; version history retained | Evidence version list, audit log |
| SEC-18 | Controlled export | Verify that export is role-restricted and preserves integrity verification output. | V5.4.1, V5.4.2, V8.2.1, V8.2.2, V14.2.1, V16.2.1, V16.3.2, V16.3.3 | Information Disclosure, Tampering, Repudiation | Unauthorized export denial = 100%; export event logging = 100%; bundle contains evidence inventory and verification outputs | Export ZIP, inventory file, audit log |
| SEC-19 | Sensitive record access | Verify that sensitive recruitment records are disclosed only to authorized roles and accesses are logged where applicable. | V8.2.2, V14.1.1, V14.2.1, V14.2.3, V15.3.1, V16.3.2 | Information Disclosure, Repudiation | Unauthorized sensitive record access block rate = 100%; sensitive access event logged | Access test result, audit log |
| SEC-20 | Audit logging and traceability | Verify that security-critical and workflow-critical actions are logged with sufficient metadata. | V16.1.1, V16.2.1, V16.2.2, V16.3.1, V16.3.2, V16.3.3, V16.4.1, V16.5.1 | Repudiation, Tampering | Audit log completeness rate = logged critical events / expected critical events; required fields present: actor, role, action, time, case, stage | Audit log export, field checklist |
| SEC-21 | Security headers and browser protections | Verify that deployed responses include browser security controls. | V3.3.1, V3.3.2, V3.4.1-V3.4.6 | Information Disclosure, Tampering | CSP present; HSTS configured in production; nosniff present; frame-ancestors denies embedding; secure cookie settings verified | Header screenshot, automated header test, deploy check |
| SEC-22 | HTTPS/TLS enforcement | Verify that client-server communication is encrypted in deployment. | V12.1.1, V12.2.1, V12.2.2 | Information Disclosure | HTTPS enforced; no insecure fallback; valid public TLS certificate in staging/production | Browser certificate screenshot, network inspection |
| SEC-23 | Secure configuration and secret handling | Verify that production configuration does not expose debug information or hard-coded secrets. | V13.3.1, V13.4.1, V13.4.2, V16.5.1 | Information Disclosure, Elevation of Privilege | Django deploy check passes; DEBUG disabled in production; secrets loaded from environment; generic error pages used | `manage.py check --deploy`, `.env.example`, error page test |
| SEC-24 | Dependency and SAST review | Verify that source code and dependencies do not contain known high-risk findings. | V15.1.1, V15.2.1, V15.3.1 | Tampering, Information Disclosure, Elevation of Privilege | Bandit high/medium findings = 0; pip-audit known vulnerabilities = 0; remediation rate = 100% for accepted fixes | Bandit report, pip-audit report, remediation notes |
| SEC-25 | Availability within prototype scope | Verify that controls reduce accidental overload without destructive testing. | V5.2.1, V6.3.1, V15.2.2 | Denial of Service | Oversized upload blocked; login throttling active; non-destructive smoke test confirms application responds | Upload rejection, lockout test, smoke test result |
| SEC-26 | Internal email change control | Verify that changes to internal user email addresses require a controlled confirmation process before the new email is applied. | V7.5.1, V6.3.7, V16.2.1, V16.3.3 | Spoofing, Information Disclosure, Repudiation | Email change is pending until verification link is used; old email remains active before verification; email-change request and verification are logged | Verification email, pending-change record, audit log, test result |

## ASVS References to Avoid Overclaiming

| ASVS Area | Treatment in RecruitGuard-CHD Matrix |
| --- | --- |
| V4 API and Web Service | Mark not applicable unless API endpoints are intentionally exposed and tested. |
| V9 Self-contained Tokens | Mark not applicable unless JWT or similar self-contained tokens are used. |
| V10 OAuth and OIDC | Mark not applicable unless OAuth/OIDC login is implemented. |
| V17 WebRTC | Mark not applicable. RecruitGuard-CHD does not use WebRTC. |
| V6.5.8 TOTP trusted time source | Applicable only if authenticator-app TOTP is implemented. Current internal MFA uses email OTP, so this should not be claimed. |
| V6.3.6 Email not used as authentication mechanism | ASVS 5.0.0 lists this as Level 3. Do not claim it for the current Level 2 email OTP design. Note the limitation if asked. |
| V5.4.3 Antivirus scanning | Claim only if antivirus scanning is implemented or tested. Current file tests cover size, signature/type validation, storage, and access control. |
| Destructive DoS testing | Excluded by the study methodology. Use bounded availability checks only. |

## Recommended Test Case Sheet Columns

| Column | Purpose |
| --- | --- |
| Test Case ID | Use SEC-01, SEC-02, etc. |
| Security Research Objective | The security objective being evaluated. |
| ASVS 5.0.0 Requirement ID | Exact ASVS ID or defensible ID range. |
| STRIDE Category | Threat category addressed. |
| Security Test / Metric | Measurable test or KPI. |
| Test Steps | Actual steps performed in staging. |
| Expected Result | Secure behavior expected. |
| Actual Result | Observed result. |
| Status | Passed, Failed, Partially Passed, Not Applicable. |
| Evidence Reference | Screenshot, video, audit log, tool report, or exported artifact. |
| Remediation Action | Fix applied or recommendation. |
