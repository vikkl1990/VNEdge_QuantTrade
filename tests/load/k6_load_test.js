// k6 load test — run with: k6 run k6_load_test.js
import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '30s', target: 10 },   // ramp to 10 users
    { duration: '1m',  target: 50 },   // ramp to 50 users
    { duration: '30s', target: 0 },    // ramp down
  ],
  thresholds: {
    'http_req_duration': ['p(95)<500'],
    'http_req_failed':   ['rate<0.01'],
  },
};

const BASE = __ENV.BASE_URL || 'http://localhost:8080';

export default function () {
  // GET endpoints (public)
  const r1 = http.get(`${BASE}/api/ping`);
  check(r1, { 'ping 200': (r) => r.status === 200 });

  const r2 = http.get(`${BASE}/api/status`);
  check(r2, { 'status 200': (r) => r.status === 200 });

  const r3 = http.get(`${BASE}/api/brain/state`);
  check(r3, { 'brain 200': (r) => r.status === 200 });

  sleep(1);
}
