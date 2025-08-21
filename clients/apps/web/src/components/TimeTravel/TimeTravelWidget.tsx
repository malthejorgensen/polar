import { OrganizationContext } from '@/providers/maintainerOrganization'
import { api } from '@/utils/client'
import { AccessTimeOutlined, RefreshOutlined } from '@mui/icons-material'
import { unwrap } from '@polar-sh/client'
import Button from '@polar-sh/ui/components/atoms/Button'
import Input from '@polar-sh/ui/components/atoms/Input'
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@polar-sh/ui/components/ui/popover'
import { Calendar } from '@polar-sh/ui/components/ui/calendar'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useContext, useState } from 'react'
import { format } from 'date-fns'

export const TimeTravelWidget = () => {
  const { organization } = useContext(OrganizationContext)
  const [isOpen, setIsOpen] = useState(false)
  const [selectedDate, setSelectedDate] = useState<Date | undefined>(new Date())
  const [hours, setHours] = useState('12')
  const [minutes, setMinutes] = useState('00')
  const queryClient = useQueryClient()

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
    refetchInterval: 5000,
  })

  const setTimeTravelMutation = useMutation({
    mutationFn: (variables: { simulated_time: string }) => {
      return api.POST('/v1/time-travel/set', {
        params: {
          query: {
            organization_id: organization.id,
          },
        },
        body: {
          simulated_time: variables.simulated_time,
          expires_in_hours: 24,
        },
      })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ['timeTravel', 'status', organization.id],
      })
      setIsOpen(false)
    },
  })

  const clearTimeTravelMutation = useMutation({
    mutationFn: () => {
      return api.DELETE('/v1/time-travel/clear', {
        params: {
          query: {
            organization_id: organization.id,
          },
        },
      })
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ['timeTravel', 'status', organization.id],
      })
    },
  })

  const handleApply = () => {
    if (!selectedDate) return

    const hoursNum = parseInt(hours, 10) || 0
    const minutesNum = parseInt(minutes, 10) || 0

    const simulatedTime = new Date(selectedDate)
    simulatedTime.setHours(hoursNum, minutesNum, 0, 0)

    setTimeTravelMutation.mutate({
      simulated_time: simulatedTime.toISOString(),
    })
  }

  const handleReset = () => {
    clearTimeTravelMutation.mutate()
  }

  const isActive = timeTravelStatus?.active

  return (
    <Popover open={isOpen} onOpenChange={setIsOpen}>
      <Button
        className={`relative h-8 w-8 ${
          isActive ? 'bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400' : ''
        }`}
        variant="ghost"
        asChild
      >
        <PopoverTrigger>
          <AccessTimeOutlined
            className="!h-5 !w-5"
            fontSize="medium"
            aria-hidden="true"
          />
          {isActive && (
            <div className="absolute right-1 top-1 h-1.5 w-1.5 rounded-full bg-blue-500" />
          )}
        </PopoverTrigger>
      </Button>
      <PopoverContent sideOffset={12} align="start" className="w-80">
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <h3 className="font-medium">Time Travel</h3>
            {isActive && (
              <span className="text-sm text-blue-600 dark:text-blue-400">
                Active
              </span>
            )}
          </div>

          {isActive && timeTravelStatus && (
            <div className="rounded-lg bg-blue-50 p-3 dark:bg-blue-900/20">
              <div className="text-sm">
                <div className="font-medium">Simulated Time:</div>
                <div className="text-gray-600 dark:text-gray-400">
                  {format(
                    new Date(timeTravelStatus.simulated_time),
                    'PPP p'
                  )}
                </div>
              </div>
            </div>
          )}

          <div className="space-y-3">
            <div>
              <label className="text-sm font-medium">Date</label>
              <div className="mt-1">
                <Calendar
                  mode="single"
                  selected={selectedDate}
                  onSelect={setSelectedDate}
                  className="rounded-md border"
                />
              </div>
            </div>

            <div className="flex space-x-2">
              <div className="flex-1">
                <label className="text-sm font-medium">Hours</label>
                <Input
                  type="number"
                  min="0"
                  max="23"
                  value={hours}
                  onChange={(e) => setHours(e.target.value)}
                  className="mt-1"
                />
              </div>
              <div className="flex-1">
                <label className="text-sm font-medium">Minutes</label>
                <Input
                  type="number"
                  min="0"
                  max="59"
                  value={minutes}
                  onChange={(e) => setMinutes(e.target.value)}
                  className="mt-1"
                />
              </div>
            </div>
          </div>

          <div className="flex space-x-2">
            <Button
              onClick={handleApply}
              disabled={!selectedDate || setTimeTravelMutation.isPending}
              className="flex-1"
            >
              {setTimeTravelMutation.isPending ? 'Applying...' : 'Apply'}
            </Button>
            <Button
              variant="outline"
              onClick={handleReset}
              disabled={clearTimeTravelMutation.isPending}
              className="flex-1"
            >
              {clearTimeTravelMutation.isPending ? (
                'Resetting...'
              ) : (
                <>
                  <RefreshOutlined className="mr-2 h-4 w-4" />
                  Reset
                </>
              )}
            </Button>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  )
}