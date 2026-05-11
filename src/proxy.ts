import type { NextRequest } from 'next/server';
import {
  adminAuthChallenge,
  adminAuthNotConfigured,
  isAdminAuthenticated,
} from '@/lib/admin-auth';

const hasAdminAuthConfigured = () =>
  Boolean(
    process.env.ADMIN_API_TOKEN ||
    (process.env.ADMIN_USERNAME && process.env.ADMIN_PASSWORD)
  );

export function proxy(request: NextRequest) {
  if (!hasAdminAuthConfigured()) {
    return adminAuthNotConfigured();
  }

  if (!isAdminAuthenticated(request)) {
    return adminAuthChallenge();
  }
}

export const config = {
  matcher: [
    '/',
    '/inventory/:path*',
    '/listings/:path*',
    '/api/ai/:path*',
    '/api/ingest',
    '/api/inventory/:path*',
  ],
};
