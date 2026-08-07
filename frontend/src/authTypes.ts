export type SystemRole = "admin" | "user";

export interface AuthUser {
  id: string;
  username: string;
  email: string;
  display_name: string;
  department: string | null;
  department_code: string | null;
  system_role: SystemRole;
  is_superuser: boolean;
  is_active: boolean;
  all_services_access: boolean;
  service_keys: string[];
  created_at: string;
  last_login_at: string | null;
}

export interface LoginPayload {
  username: string;
  password: string;
}

export interface SeelisLoginPayload {
  userId: string;
  pswd: string;
}

export interface RegisterPayload {
  username: string;
  display_name: string;
  email: string;
  password: string;
}

export interface UserAccessUpdate {
  system_role: SystemRole;
  is_superuser: boolean;
  is_active: boolean;
  all_services_access: boolean;
  service_keys: string[];
}

export interface AuthUserResponse {
  user: AuthUser;
}

export interface ServiceSummary {
  key: string;
  name: string;
  description: string;
  is_active: boolean;
}

export interface UsersResponse {
  users: AuthUser[];
  services: ServiceSummary[];
}
