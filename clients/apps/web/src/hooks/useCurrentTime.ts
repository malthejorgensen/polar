import { OrganizationContext } from '@/providers/maintainerOrganization'
import { api } from '@/utils/client'
import { unwrap } from '@polar-sh/client'
import { useQuery } from '@tanstack/react-query'
import { useContext } from 'react'

export const useCurrentTime = () => {
  const { organization } = useContext(OrganizationContext)

  const { data: timeTravelStatus } = useQuery({
    queryKey: ['timeTravel', 'status', organization.id],
    queryFn: () =>
      unwrap(
        api.GET('/v1/time-travel/status', {
          params: {
            query: {
              organization_id: organization.id,
            },
          },
        }),
      ),
    retry: false, // Don't retry on auth errors
  })

  // Return simulated time if active, otherwise return current real time
  if (timeTravelStatus?.active && timeTravelStatus.simulated_time) {
    return new Date(timeTravelStatus.simulated_time)
  }

  return new Date()
}
