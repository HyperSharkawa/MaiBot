import axios from 'axios'

const apiClient = axios.create({
  baseURL: import.meta.env.DEV ? 'http://localhost:8000' : '',
  timeout: 10000,
})

export default apiClient
