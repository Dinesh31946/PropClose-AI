import { NextResponse } from 'next/server';

const ADMIN_REALM = 'PropClose Admin';

const decodeBasicAuth = (authorization: string): { username: string; password: string } | null => {
  if (!authorization.startsWith('Basic ')) return null;

  try {
    const decoded = atob(authorization.slice('Basic '.length).trim());
    const separatorIndex = decoded.indexOf(':');

    if (separatorIndex === -1) return null;

    return {
      username: decoded.slice(0, separatorIndex),
      password: decoded.slice(separatorIndex + 1),
    };
  } catch {
    return null;
  }
};

const hasAdminCredentialsConfigured = () =>
  Boolean(
    process.env.ADMIN_API_TOKEN ||
    (process.env.ADMIN_USERNAME && process.env.ADMIN_PASSWORD)
  );

export const isAdminAuthenticated = (request: Pick<Request, 'headers'>): boolean => {
  const authorization = request.headers.get('authorization') || '';
  const apiToken = process.env.ADMIN_API_TOKEN;

  if (apiToken && authorization === `Bearer ${apiToken}`) {
    return true;
  }

  const basicAuth = decodeBasicAuth(authorization);
  if (!basicAuth) return false;

  return (
    Boolean(process.env.ADMIN_USERNAME && process.env.ADMIN_PASSWORD) &&
    basicAuth.username === process.env.ADMIN_USERNAME &&
    basicAuth.password === process.env.ADMIN_PASSWORD
  );
};

export const requireAdmin = (request: Pick<Request, 'headers'>): NextResponse | null => {
  if (isAdminAuthenticated(request)) return null;

  const configured = hasAdminCredentialsConfigured();

  return NextResponse.json(
    {
      success: false,
      error: configured
        ? 'Admin authentication is required'
        : 'Admin authentication is not configured',
    },
    {
      status: configured ? 401 : 503,
      headers: {
        'WWW-Authenticate': `Basic realm="${ADMIN_REALM}", charset="UTF-8"`,
      },
    }
  );
};

export const adminAuthChallenge = () =>
  new NextResponse('Admin authentication is required', {
    status: 401,
    headers: {
      'WWW-Authenticate': `Basic realm="${ADMIN_REALM}", charset="UTF-8"`,
    },
  });

export const adminAuthNotConfigured = () =>
  new NextResponse('Admin authentication is not configured', { status: 503 });
