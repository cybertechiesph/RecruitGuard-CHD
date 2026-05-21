# Security Testing Results Template

Use this template for Chapter 4 evidence. Run tests only against the approved
staging environment with dummy or synthetic data.

## Test Environment

- Date:
- Tester:
- Environment: local / staging
- Application URL:
- Commit or version:
- Dataset: dummy/synthetic only

## Tool-Based Results

| Tool | Scope | Command or Activity | Result Summary | Critical/High Findings | Remediation Status |
| --- | --- | --- | --- | --- | --- |
| Bandit | Python code | `python -m bandit -r config recruitment -x recruitment/migrations,recruitment/tests.py` |  |  |  |
| pip-audit | Python dependencies | `python -m pip_audit` |  |  |  |
| OWASP ZAP | Deployed web app | Baseline/authenticated scan on staging |  |  |  |
| Burp Suite Community | Manual access-control checks | Request/response inspection |  |  |  |

## Control Verification Matrix

| Test ID | Control Area | ASVS 5.0.0 Requirement ID | STRIDE Category | Test Performed | Metric / KPI | Expected Result | Actual Result | Evidence Source |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SEC-06 | Applicant OTP | V6.5.1, V6.5.3-V6.5.5, V6.6.2, V6.6.3 | Spoofing, Tampering | Submit without valid OTP | OTP bypass success rate = 0% | Submission blocked |  | Test record / screenshot / log |
| SEC-07 | Internal MFA | V6.3.3, V6.5.1, V6.5.3-V6.5.5, V6.6.2, V6.6.3 | Spoofing | Login without email OTP | MFA bypass success rate = 0% | Internal access blocked |  | Audit log / test result |
| SEC-10 | RBAC | V8.1.1, V8.2.1, V8.2.2, V8.3.1 | Elevation of Privilege, Information Disclosure | Access another role's restricted page | Unauthorized access block rate = 100% | Access denied |  | Response code / screenshot |
| SEC-11 | Level-Aware Routing | V2.2.1, V2.3.1, V2.3.2, V8.2.1, V8.2.2 | Elevation of Privilege, Tampering | Secretariat processes Level 2 case | Level restriction enforcement rate = 100% | Action blocked |  | Test record / audit log |
| SEC-12 | Workflow Integrity | V2.3.1-V2.3.3, V8.3.1, V16.3.3 | Tampering, Repudiation | Skip required stage | Workflow enforcement success rate = 100% | Transition blocked |  | Test record / audit log |
| SEC-13A | SQL Injection Resistance | V1.2.4, V2.1.1, V2.2.1 | Tampering, Information Disclosure | Submit SQL injection payloads in database-backed inputs | SQL injection success rate = 0% | No SQL injection succeeds and no database error is exposed |  | Burp/ZAP/manual request evidence |
| SEC-13B | General Input Validation | V1.2.1-V1.2.3, V1.3.3, V2.1.1, V2.2.1 | Tampering | Submit invalid scores, dates, statuses, and malformed inputs | Invalid input rejection rate = 100% | Invalid values are rejected or safely handled |  | Validation screenshot / test result |
| SEC-16 | Evidence Integrity | V5.3.2, V11.2.1, V11.4.1, V16.2.1 | Tampering, Repudiation | Alter exported evidence file | Hash verification pass/fail result | Hash verification fails |  | Export verification output |
| SEC-18 | Export Control | V5.4.1, V5.4.2, V8.2.1, V8.2.2, V16.3.2 | Information Disclosure, Repudiation | Unauthorized export attempt | Unauthorized export denial rate = 100% | Export blocked |  | Response code / audit log |
| SEC-20 | Audit Logging | V16.1.1, V16.2.1, V16.3.1-V16.3.3 | Repudiation | Execute critical workflow action | Audit log completeness rate = 100% | Actor, role, time, case, and stage recorded |  | Audit log export |
| SEC-08 | Login Throttling | V6.1.1, V6.3.1, V16.3.1, V16.3.3 | Spoofing | Repeated bad password attempts | Lockout at configured threshold | Temporary lockout applied |  | Audit log / test result |
| SEC-26 | Email Change Control | V7.5.1, V6.3.7, V16.2.1, V16.3.3 | Spoofing, Information Disclosure, Repudiation | Change internal account email | Email is not changed until verification link is used | Email change remains pending before verification |  | Verification email / audit log |

See `docs/asvs-security-testing-matrix.md` for the full ASVS-specific mapping.

## Security Metrics

| Metric | Formula / Basis | Result |
| --- | --- | --- |
| Vulnerability Count by Severity | Count of findings grouped by severity |  |
| Remediation Rate | Remediated findings / total findings |  |
| Unauthorized Access Attempt Block Rate | Blocked unauthorized attempts / total unauthorized attempts |  |
| Level Restriction Enforcement Rate | Blocked invalid level actions / total invalid level actions |  |
| Workflow Enforcement Success Rate | Blocked invalid transitions / total invalid transitions |  |
| Audit Log Completeness Rate | Logged critical events / expected critical events |  |
| Export Integrity Verification Pass Rate | Valid exports / tested exports |  |
| OTP Verification Enforcement Rate | Blocked invalid OTP attempts / invalid OTP attempts |  |

## Acceptance Decision

- No unresolved Critical findings:
- No unresolved High findings:
- Remaining Medium/Low findings and justification:
- Final security testing conclusion:
