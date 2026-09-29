import axios from 'axios'

const api = axios.create({
  baseURL: 'http://127.0.0.1:8000',
  timeout: 10000,
  headers: { 'Content-Type': 'application/json' },
})

export async function getHealth() {
  const { data } = await api.get('/health')
  return data
}

export async function getApiHealth() {
  const { data } = await api.get('/api/health')
  return data
}

export default api
