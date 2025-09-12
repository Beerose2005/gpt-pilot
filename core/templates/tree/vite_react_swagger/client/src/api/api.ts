import axios, { AxiosRequestConfig, AxiosError, InternalAxiosRequestConfig, AxiosInstance } from 'axios';
import JSONbig from 'json-bigint';

{% if options.auth_type == "api_key" %}
const API_KEY = import.meta.env.VITE_API_KEY;
{% endif %}

{% if options.auth_type != "login" %}
const EXTERNAL_API_URL = import.meta.env.VITE_EXTERNAL_API_URL;
{% endif %}

const localApi = axios.create({
  headers: {
    'Content-Type': 'application/json',
  },
  validateStatus: (status) => {
    return status >= 200 && status < 300;
  },
  transformResponse: [(data) => JSONbig.parse(data)]
});

{% if options.auth_type != "login" %}
const externalApi = axios.create({
  baseURL: EXTERNAL_API_URL,
  headers: {
    'Content-Type': 'application/json',
  },
  validateStatus: (status) => {
    return status >= 200 && status < 300;
  },
});
{% endif %}


let accessToken: string | null = null;

const getApiInstance = (url: string) => {
  {% if options.auth_type != "login" %}
return isAuthEndpoint(url) ? localApi : externalApi;
  {% else %}
  return localApi;
  {% endif %}
};

const isAuthEndpoint = (url: string): boolean => {
  return url.includes("/api/auth");
};

{% if options.auth %}
// Check if the URL is for the refresh token endpoint to avoid infinite loops
const isRefreshTokenEndpoint = (url: string): boolean => {
  return url.includes("/api/auth/refresh");
};

const setupInterceptors = (apiInstance: typeof axios) => {
  apiInstance.interceptors.request.use(
    (config: InternalAxiosRequestConfig): InternalAxiosRequestConfig => {
{% if options.auth_type == "api_key" %}
      if (!isAuthEndpoint(config.url || '')) {
          config.baseURL = EXTERNAL_API_URL;
        if (config.headers && API_KEY) {
          config.headers['api_key'] = API_KEY;
        }
      }
{% endif %}

      if (!accessToken) {
        accessToken = localStorage.getItem('accessToken');
      }
      if (accessToken && config.headers) {
        config.headers.Authorization = `Bearer ${accessToken}`;
      }

      return config;
    },
    (error: AxiosError): Promise<AxiosError> => Promise.reject(error)
  );

    {% if options.auth %}
  apiInstance.interceptors.response.use(
    (response) => response,
    async (error: AxiosError): Promise<any> => {
      const originalRequest = error.config as InternalAxiosRequestConfig & { _retry?: boolean };

      // Only refresh token when we get a 401/403 error (token is invalid/expired)
      if (error.response?.status && [401, 403].includes(error.response.status) &&
          !originalRequest._retry &&
          originalRequest.url && !isRefreshTokenEndpoint(originalRequest.url)) {
        originalRequest._retry = true;

        try {
            const refreshToken = localStorage.getItem('refreshToken');
            if (!refreshToken) {
              throw new Error('No refresh token available');
            }

            const response = await localApi.post(`/api/auth/refresh`, {
              refreshToken,
            });

            if (response.data.data) {
            const newAccessToken = response.data.data.accessToken;
            const newRefreshToken = response.data.data.refreshToken;

            localStorage.setItem('accessToken', newAccessToken);
            localStorage.setItem('refreshToken', newRefreshToken);
            accessToken = newAccessToken;

            if (originalRequest.headers) {
              originalRequest.headers.Authorization = `Bearer ${newAccessToken}`;
            }
          } else {
            throw new Error('Invalid response from refresh token endpoint');
          }

          if (originalRequest.headers) {
            originalRequest.headers.Authorization = `Bearer ${accessToken}`;
            {% if options.auth_type == "api_key" %}
            if (!isAuthEndpoint(originalRequest.url || '') && API_KEY) {
              originalRequest.headers['api_key'] = API_KEY;
            }
            {% endif %}
          }
          return getApiInstance(originalRequest.url || '')(originalRequest);
        } catch (err) {
          localStorage.removeItem('refreshToken');
          localStorage.removeItem('accessToken');
          accessToken = null;
          window.location.href = '/login';
          return Promise.reject(err);
        }
      }

      return Promise.reject(error);
    }
  );
    {% endif %}
};

setupInterceptors(localApi);

{% if options.auth_type != "login" %}
setupInterceptors(externalApi);
{% endif %}

{% endif %}

const api = {
  request: (config: AxiosRequestConfig) => {
    const apiInstance = getApiInstance(config.url || '');
    return apiInstance(config);
  },
  get: (url: string, config?: AxiosRequestConfig) => {
    const apiInstance = getApiInstance(url);
    return apiInstance.get(url, config);
  },
  post: (url: string, data?: any, config?: AxiosRequestConfig) => {
    const apiInstance = getApiInstance(url);
    return apiInstance.post(url, data, config);
  },
  put: (url: string, data?: any, config?: AxiosRequestConfig) => {
    const apiInstance = getApiInstance(url);
    return apiInstance.put(url, data, config);
  },
  delete: (url: string, config?: AxiosRequestConfig) => {
    const apiInstance = getApiInstance(url);
    return apiInstance.delete(url, config);
  },
};

export default api;
